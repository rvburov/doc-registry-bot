"""Резервная копия: DUMP всей базы и CSV журнала КД одним снимком базы.

Бот файлов не пишет и доступа к диску компьютера не имеет. Он открывает транзакцию
REPEATABLE READ, экспортирует ее снимок (`pg_export_snapshot`), в этой же транзакции выгружает
журнал в CSV и ставит заявку контейнеру копий (таблица `backup_jobs`). Контейнер делает
`pg_dump --snapshot` этим же снимком, поэтому DUMP и CSV совпадают точно, даже если в эти
секунды кто-то регистрирует номер. Транзакция со снимком держится открытой, пока контейнер не
закончит: снимок живет, пока жива транзакция.

Копия успешна, только если сделаны оба файла. Итог пишется в ту же строку `backup_jobs`, по ней
замена журнала проверяет свежесть и отпечаток копии.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from doc_registry_bot import importer
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import AuditEntry, BackupJob
from doc_registry_bot.registry.folders import Choice, shown_path

# Контейнер копий раз в 10 секунд пишет в базу отметку «жив» (bot_state, backup_worker).
# Отметка старше ALIVE_SECONDS — контейнер остановлен: заявку некому выполнять.
ALIVE_SECONDS = 45.0
# Сколько ждать: живого контейнера, если он остановлен; очереди, если он занят другой копией;
# выполнения заявки.
PICKUP_SECONDS = 60.0
QUEUE_SECONDS = 30 * 60.0
RUN_SECONDS = 60 * 60.0
POLL_SECONDS = 1.0
WORKER = "backup_worker"
NOT_PICKED = (
    "контейнер копий не ответил: проверьте, что он запущен "
    "(docker compose ps, строка backup) и обновлен до этой версии бота"
)
QUEUE_BUSY = "контейнер копий занят другой копией дольше 30 минут"
STOPPED = "контейнер копий перестал отвечать во время копии"
TOO_LONG = "копия не закончилась за час"


@dataclass(frozen=True)
class Request:
    """Кто и куда просит копию."""

    choice: Choice
    source: str = "chat"
    requested_by: int = 0
    requested_fio: str = ""
    owner_uid: int | None = None
    owner_gid: int | None = None


def file_names(moment: datetime, zone: ZoneInfo) -> tuple[str, str]:
    stamp = moment.astimezone(zone).strftime("%Y-%m-%d_%H%M%S")
    return f"база-{stamp}.dump", f"журнал-{stamp}.csv"


def _engine(sessions: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    engine = sessions.kw.get("bind")
    if not isinstance(engine, AsyncEngine):
        raise RuntimeError("фабрика сессий без движка базы")
    return engine


async def make(
    sessions: async_sessionmaker[AsyncSession],
    zone: ZoneInfo,
    request: Request,
    now: datetime,
    *,
    job_id: int | None = None,
) -> BackupJob:
    """Сделать копию и дождаться итога. `job_id` — заявка, уже записанная со статусом
    requested (из чата); иначе заявка создается здесь (терминал)."""
    async with _engine(sessions).connect() as connection:
        snapshot_connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with snapshot_connection.begin():
            snapshot = (
                await snapshot_connection.execute(sql("SELECT pg_export_snapshot()"))
            ).scalar_one()
            async with AsyncSession(bind=snapshot_connection) as reader:
                data = await importer.export_csv(reader, zone)
                rows = await journal.count(reader)
            job_id = await _enqueue(
                sessions,
                request,
                now=now,
                zone=zone,
                snapshot=snapshot,
                data=data,
                rows=rows,
                job_id=job_id,
            )
            return await wait(sessions, job_id)


async def _enqueue(
    sessions: async_sessionmaker[AsyncSession],
    request: Request,
    *,
    now: datetime,
    zone: ZoneInfo,
    snapshot: str,
    data: bytes,
    rows: int,
    job_id: int | None,
) -> int:
    dump_name, csv_name = file_names(now, zone)
    values = {
        "status": "new",
        "snapshot": snapshot,
        "csv_data": data,
        "fingerprint": importer.fingerprint(data),
        "rows": rows,
        "dump_name": dump_name,
        "csv_name": csv_name,
        "folder_key": request.choice.folder.key,
        "folder_label": request.choice.label(),
        "subdir": request.choice.subdir,
    }
    async with sessions() as session, session.begin():
        if job_id is not None:
            changed = await session.scalar(
                update(BackupJob)
                .where(BackupJob.id == job_id, BackupJob.status == "requested")
                .values(**values)
                .returning(BackupJob.id)
            )
            if changed is None:
                raise RuntimeError(f"заявка на копию {job_id} уже не ждет выполнения")
            return job_id
        job = BackupJob(
            kind="backup",
            source=request.source,
            requested_by=request.requested_by,
            requested_fio=request.requested_fio,
            owner_uid=request.owner_uid,
            owner_gid=request.owner_gid,
            created_at=now,
            **values,
        )
        session.add(job)
        await session.flush()
        return job.id


async def worker_age(sessions: async_sessionmaker[AsyncSession]) -> float | None:
    """Сколько секунд назад контейнер копий отметился живым (по часам базы); None — ни разу."""
    async with sessions() as session:
        value = await session.scalar(
            sql(
                "SELECT extract(epoch FROM now() - value::timestamptz) FROM bot_state "
                "WHERE key = :key"
            ),
            {"key": WORKER},
        )
    return float(value) if value is not None else None


async def wait(sessions: async_sessionmaker[AsyncSession], job_id: int) -> BackupJob:
    """Ждать итога заявки. Остановленный контейнер отличается от занятого по отметке «жив»:
    заявку отменяет только остановленный контейнер или слишком долгая очередь."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        async with sessions() as session:
            job = await session.get(BackupJob, job_id)
        if job is None:
            raise RuntimeError(f"заявка на копию {job_id} пропала")
        if job.status in ("done", "failed", "canceled"):
            return job
        waited = loop.time() - started
        if waited > PICKUP_SECONDS:
            age = await worker_age(sessions)
            stopped = age is None or age > ALIVE_SECONDS
            # Заявку могли взять или закончить за это время: _close меняет только ожидаемый
            # статус, а итог перечитывается на следующем круге.
            if job.status == "new" and (stopped or waited > QUEUE_SECONDS):
                await _close(
                    sessions, job_id, ("new",), "canceled", NOT_PICKED if stopped else QUEUE_BUSY
                )
                continue
            if job.status == "running" and (stopped or waited > RUN_SECONDS):
                await _close(
                    sessions, job_id, ("running",), "failed", STOPPED if stopped else TOO_LONG
                )
                continue
        await asyncio.sleep(POLL_SECONDS)


async def _close(
    sessions: async_sessionmaker[AsyncSession],
    job_id: int,
    statuses: tuple[str, ...],
    status: str,
    message: str,
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            update(BackupJob)
            .where(BackupJob.id == job_id, BackupJob.status.in_(statuses))
            .values(
                status=status,
                message=message,
                csv_data=None,
                snapshot=None,
                finished_at=func.now(),
            )
        )


async def verify(
    sessions: async_sessionmaker[AsyncSession], backup: BackupJob, now: datetime
) -> BackupJob:
    """Попросить контейнер проверить файлы копии: оба на месте, DUMP читается, CSV совпадает
    с отпечатком."""
    async with sessions() as session, session.begin():
        job = BackupJob(
            kind="verify",
            target_id=backup.id,
            source="chat",
            requested_by=backup.requested_by,
            folder_key=backup.folder_key,
            folder_label=backup.folder_label,
            subdir=backup.subdir,
            dump_name=backup.dump_name,
            csv_name=backup.csv_name,
            fingerprint=backup.fingerprint,
            dump_sha256=backup.dump_sha256,
            rows=backup.rows,
            status="new",
            created_at=now,
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    return await wait(sessions, job_id)


async def latest_successful(session: AsyncSession, now: datetime) -> BackupJob | None:
    """Последняя успешная копия не старше 30 минут (из чата или терминала)."""
    found = await session.scalars(
        select(BackupJob)
        .where(
            BackupJob.kind == "backup",
            BackupJob.status == "done",
            BackupJob.dump_ok.is_(True),
            BackupJob.csv_ok.is_(True),
            BackupJob.created_at >= now - importer.FRESH,
        )
        .order_by(BackupJob.created_at.desc(), BackupJob.id.desc())
        .limit(1)
    )
    return found.first()


def size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} МБ".replace(".", ",")
    return f"{max(1, round(value / 1024))} КБ"


def where(job: BackupJob) -> str:
    """Папка копии для отчета. Путь — тот, куда контейнер копий на самом деле записал файлы (из
    его настроек), если он отличается от показанного в чате."""
    label = job.folder_label or job.folder_key
    if job.host_path and job.folder_key in ("1", "2", "3"):
        actual = shown_path(job.host_path)
        if job.subdir:
            separator = "\\" if ":" in actual else "/"
            actual = actual.rstrip("\\/") + separator + job.subdir.replace("/", separator)
        if actual not in label:
            return f"{label}; контейнер копий записал в {actual}"
    return label


async def fail(sessions: async_sessionmaker[AsyncSession], job_id: int, message: str) -> BackupJob:
    """Заявка не выполнена по вине бота: отметить, если контейнер ее еще не закончил."""
    await _close(sessions, job_id, ("requested", "new", "running"), "failed", message)
    async with sessions() as session:
        job = await session.get(BackupJob, job_id)
    if job is None:
        raise RuntimeError(f"заявка на копию {job_id} пропала")
    return job


async def reported(
    sessions: async_sessionmaker[AsyncSession],
    job_id: int,
    now: datetime,
    actor_fio: str,
    details: str,
) -> None:
    """Итог отправлен владельцу: отметка и запись в журнал действий."""
    async with sessions() as session, session.begin():
        job = await session.get(BackupJob, job_id, with_for_update=True)
        if job is None:
            return
        job.reported_at = now
        session.add(
            AuditEntry(
                at=now,
                actor_id=job.requested_by,
                actor_fio=actor_fio or job.requested_fio,
                action="Резервная копия",
                details=details,
            )
        )
