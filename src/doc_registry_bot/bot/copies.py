"""Резервная копия и замена журнала через чат. Только главный администратор (владелец из
OWNER_IDS): обычные администраторы и пользователи этих прав не получают.

Резервная копия: бот показывает папку по умолчанию и список разрешенных папок, ждет «Да»,
номер или путь другой папки, «Нет». После «Да» копия делается после коммита, вне очереди
сообщений; итог по каждому файлу приходит отдельным сообщением.

Замена журнала: файл CSV в сообщении «Заменить журнал». Бот проверяет файл по правилам
импорта, требует свежую успешную копию (оба файла, не старше 30 минут, журнал с тех пор не
менялся), предупреждает и ждет отдельного «Да». Перед самой заменой копия проверяется снова:
файлы на месте и целы, свежесть и отпечаток — уже под блокировкой журнала.

Еженедельное напоминание приходит всем владельцам; первый ответ закрывает его у всех.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot import backups, importer
from doc_registry_bot.bot import pending
from doc_registry_bot.bot.context import Ctx, Task
from doc_registry_bot.bot.registration import refused, refused_headed
from doc_registry_bot.bot.types import MessengerError, Reply
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import (
    AuditEntry,
    BackupJob,
    JournalUpload,
    Member,
    Pending,
    Reminder,
)
from doc_registry_bot.registry import markup, text
from doc_registry_bot.registry.folders import Choice, Folder, choose
from doc_registry_bot.registry.markup import bold, escape

log = logging.getLogger(__name__)

BACKUP = "резервная копия"
REPLACE = "заменить журнал"
BACKUP_KIND = "backup"
REPLACE_KIND = "replace_journal"
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_SHOWN_PROBLEMS = 20
REMINDER_TEXT = "Необходимо сделать резервную копию. Сделать сейчас? Да / Нет"
REMINDER_WINDOW = timedelta(hours=24)
# Часы мессенджера и бота могут немного расходиться.
REMINDER_CLOCK_SKEW = timedelta(seconds=5)
NO_FOLDERS = (
    "Резервная копия: в .env не задано ни одной папки для копий (BACKUP_DIR_1). Впишите путь и "
    "выполните docker compose up -d"
)


async def name_of(ctx: Ctx, user_id: int) -> str:
    """ФИО человека: из списка людей бота, иначе из карточки мессенджера (у главного
    администратора записи в списке может не быть), иначе ID."""
    member = await ctx.session.get(Member, user_id)
    if member is not None and member.fio:
        return member.fio
    try:
        card = await ctx.messenger.user_card(user_id)
    except MessengerError:
        card = None
    return text.collapse(card.fio) if card is not None and card.fio else f"ID {user_id}"


# ── Резервная копия ───────────────────────────────────────────────────────────


def _folder(ctx: Ctx, key: str) -> Folder | None:
    return next((folder for folder in ctx.config.folders if folder.key == key), None)


def _prompt(ctx: Ctx, choice: Choice) -> str:
    lines = [
        bold("Резервная копия: файл базы (DUMP) и журнал КД (CSV)"),
        f"Папка: {bold(escape(choice.label()))}",
        "",
        "Папки для копий:",
        *(
            f"{number}. {escape(folder.label())}"
            for number, folder in enumerate(ctx.config.folders, start=1)
        ),
        "",
        "Ответьте: <b>Да</b> сохранить в эту папку, номер, имя или путь другой папки из списка "
        "(можно с подпапкой) или <b>Нет</b>.",
        markup.LIFETIME,
    ]
    return "\n".join(lines)


async def ask_backup(ctx: Ctx, source: str, first: Choice | None = None) -> None:
    """Показать папку по умолчанию и список, ждать ответа."""
    if not ctx.config.folders:
        refused(ctx, NO_FOLDERS)
        return
    choice = first or Choice(ctx.config.folders[0])
    await pending.put(
        ctx,
        BACKUP_KIND,
        {"key": choice.folder.key, "subdir": choice.subdir, "source": source},
    )
    ctx.reply(_prompt(ctx, choice))


async def backup_command(ctx: Ctx, argument: str) -> None:
    """«Резервная копия» или «Резервная копия: 2» (сразу с выбором папки)."""
    if not ctx.config.folders:
        refused(ctx, NO_FOLDERS)
        return
    first: Choice | None = None
    if argument:
        found = choose(list(ctx.config.folders), argument)
        if isinstance(found, str):
            refused(ctx, found)
            return
        first = found
    await ask_backup(ctx, "chat", first)


async def answer_backup(
    ctx: Ctx, found: Pending, plain: str, answer: bool | None, *, other: bool
) -> bool:
    """Ответ на вопрос о папке. `other` — это команда или файл, а не ответ: False."""
    payload: dict[str, Any] = dict(found.payload)
    if answer is None:
        if other or not plain.strip():
            return False
        chosen = choose(list(ctx.config.folders), plain)
        if isinstance(chosen, str):
            refused(ctx, f"{chosen}\nОтветьте: Да, номер другой папки из списка или Нет")
            return True
        payload.update(key=chosen.folder.key, subdir=chosen.subdir)
        await pending.put(ctx, BACKUP_KIND, payload)
        ctx.reply(_prompt(ctx, chosen))
        return True
    await pending.take(ctx, found)
    if answer is False:
        ctx.reply(bold("Операция отменена. Резервная копия не делалась"))
        if payload.get("source") == "reminder":
            who = escape(ctx.sender_fio or await name_of(ctx, ctx.sender_id))
            for owner in ctx.config.owner_ids:
                if owner != ctx.sender_id:
                    ctx.send_to(owner, f"{who}: копия по напоминанию отменена, не сделана.")
        return True
    folder = _folder(ctx, str(payload["key"]))
    if folder is None:
        refused(ctx, "Папка больше не задана в настройках сервера. Начните заново: Резервная копия")
        return True
    choice = Choice(folder, str(payload.get("subdir", "")))
    source = str(payload.get("source", "chat"))
    ctx.sender_fio = ctx.sender_fio or await name_of(ctx, ctx.sender_id)
    job = BackupJob(
        kind="backup",
        source=source,
        requested_by=ctx.sender_id,
        requested_fio=ctx.sender_fio,
        folder_key=folder.key,
        folder_label=choice.label(),
        subdir=choice.subdir,
        status="requested",
        created_at=ctx.now,
    )
    ctx.session.add(job)
    await ctx.session.flush()
    ctx.reply(
        bold("Резервная копия делается")
        + f"\nПапка: {escape(choice.label())}\nИтог придет отдельным сообщением."
    )
    request = backups.Request(choice, source, ctx.sender_id, ctx.sender_fio)
    ctx.later(_backup_task(ctx, job.id, request))
    return True


def _backup_task(ctx: Ctx, job_id: int, request: backups.Request) -> Task:
    """Сделать копию. Итог остается в строке заявки, отправляет его `claim_reports`: так он
    доходит и тогда, когда отправка сорвалась или бот перезапустился."""
    sessions, zone, now = ctx.sessions, ctx.config.zone, ctx.now
    assert sessions is not None

    async def run() -> list[Reply]:
        try:
            await backups.make(sessions, zone, request, now, job_id=job_id)
        except Exception:
            log.exception("Резервная копия %s не сделана", job_id)
            await backups.fail(sessions, job_id, "внутренняя ошибка бота")
        return []

    return run


def report(job: BackupJob, zone: Any) -> str:
    """Итог копии для владельца: по каждому файлу, создан ли и где лежит."""
    ok = job.successful
    lines = [bold("Резервная копия сделана" if ok else "Резервная копия не сделана")]
    lines.append(f"Папка: {escape(backups.where(job))}")
    if job.dump_ok:
        lines.append(f"DUMP базы: создан, {escape(job.dump_name)}, {backups.size(job.dump_size)}")
    else:
        lines.append("DUMP базы: не создан")
    if job.csv_ok:
        lines.append(
            f"CSV журнала: создан, {escape(job.csv_name)}, {backups.size(job.csv_size)}, "
            f"записей: {job.rows}"
        )
    else:
        lines.append("CSV журнала: не создан")
    if job.message:
        lines.append(f"Причина: {escape(job.message)}")
    if ok:
        lines.append("Для замены журнала копия годится 30 минут.")
    else:
        lines.append(
            "Копия считается сделанной, только если созданы оба файла. Повторите: Резервная копия"
        )
    return "\n".join(lines)


def report_line(job: BackupJob) -> str:
    """Итог копии одной строкой для журнала действий."""
    state = "сделана" if job.successful else "не сделана"
    files = ", ".join(
        name for name, ok in ((job.dump_name, job.dump_ok), (job.csv_name, job.csv_ok)) if ok
    )
    detail = f"{state}: {backups.where(job)}"
    if files:
        detail += f"; файлы: {files}"
    if job.message:
        detail += f"; {job.message}"
    return detail


# ── Напоминание ──────────────────────────────────────────────────────────────


async def answer_reminder(ctx: Ctx, answer: bool) -> bool:
    """«Да» или «Нет» на еженедельное напоминание. False — открытого напоминания нет."""
    mine = await ctx.session.scalar(
        select(Reminder)
        .where(
            Reminder.owner_id == ctx.sender_id,
            Reminder.status == "sent",
            Reminder.sent_at >= ctx.now - REMINDER_WINDOW,
        )
        .order_by(Reminder.slot_at.desc())
        .limit(1)
    )
    if mine is None:
        return False
    # После напоминания владельцу задали другой вопрос: «Да» — скорее к нему. Напоминание
    # повторится, когда владелец освободится (служба), и тогда ответ будет однозначным.
    asked = await pending.asked_at(ctx.session, ctx.sender_id)
    if asked is not None and mine.sent_at is not None and asked > mine.sent_at:
        return False
    # «Да», написанное до напоминания (дочитано после простоя бота), к нему не относится.
    if (
        ctx.sent_at is not None
        and mine.sent_at is not None
        and ctx.sent_at < mine.sent_at - REMINDER_CLOCK_SKEW
    ):
        return False
    rows = list(
        await ctx.session.scalars(
            select(Reminder).where(Reminder.slot_at == mine.slot_at).with_for_update()
        )
    )
    closed = next((row for row in rows if row.closed_at is not None), None)
    if closed is not None:
        if closed.answered_by == ctx.sender_id:
            return False
        who = "другой главный администратор"
        if closed.answered_by is not None:
            who = await name_of(ctx, closed.answered_by)
        ctx.reply(
            bold("На напоминание уже ответили")
            + f"\nОтветил {escape(who)}: {escape(closed.answer.capitalize())}"
        )
        return True
    word = "да" if answer else "нет"
    for row in rows:
        row.closed_at = ctx.now
        row.answer = word
        row.answered_by = ctx.sender_id
    others = [
        row.owner_id for row in rows if row.owner_id != ctx.sender_id and row.status == "sent"
    ]
    ctx.sender_fio = ctx.sender_fio or await name_of(ctx, ctx.sender_id)
    who = escape(ctx.sender_fio)
    ctx.audit("Напоминание о резервной копии", f"ответ: {word}")
    if answer:
        for owner in others:
            ctx.send_to(owner, f"Резервную копию по напоминанию делает {who}.")
        await ask_backup(ctx, "reminder")
    else:
        for owner in others:
            ctx.send_to(
                owner, f"{who}: ответ Нет на напоминание, копия на этой неделе не делается."
            )
        lines = [bold("Резервная копия не делается")]
        weekly = ctx.config.reminder
        if weekly is not None:
            when = weekly.following(ctx.now).strftime("%d.%m.%Y %H:%M")
            if weekly.zone.key != ctx.config.zone.key:
                when += f" ({weekly.zone.key})"
            lines.append(f"Следующее напоминание: {when}")
        ctx.reply("\n".join(lines))
    return True


# ── Замена журнала ───────────────────────────────────────────────────────────


def _problems(problems: list[str]) -> str:
    shown = problems[:MAX_SHOWN_PROBLEMS]
    rest = len(problems) - len(shown)
    return "\n".join(shown + ([f"И еще ошибок: {rest}"] if rest > 0 else []))


async def replace_command(ctx: Ctx, file_ids: list[int], message_id: int) -> None:
    if len(file_ids) != 1:
        refused(ctx, "Заменить журнал: приложите к этому же сообщению один файл CSV журнала")
        return
    try:
        attachment = await ctx.messenger.download(message_id, file_ids[0])
    except MessengerError:
        refused_headed(ctx, "Файл не получен. Отправьте его заново")
        return
    name = (attachment.name or "").strip()
    if not name.lower().endswith(".csv"):
        refused(ctx, f"Файл: для замены журнала нужен файл .csv, а прислан «{name or 'без имени'}»")
        return
    if len(attachment.data) > MAX_CSV_BYTES:
        refused(ctx, f"Файл: больше допустимых {MAX_CSV_BYTES // 1024 // 1024} МБ")
        return
    zone = ctx.config.zone
    _, checked = importer.read_journal(attachment.data, name, zone, ctx.now)
    if not checked.ok:
        refused_headed(
            ctx,
            "Журнал не заменен: в файле ошибки, исправьте их и отправьте файл снова\n"
            + _problems(checked.problems),
        )
        return
    backup = await backups.latest_successful(ctx.session, ctx.now)
    if backup is None:
        refused_headed(
            ctx,
            "Журнал не заменен: нет свежей резервной копии (оба файла, не старше 30 минут). "
            "Сделайте копию командой Резервная копия и отправьте файл снова",
        )
        return
    current = importer.fingerprint(await importer.export_csv(ctx.session, zone))
    if current != backup.fingerprint:
        refused_headed(
            ctx,
            "Журнал не заменен: журнал изменился после резервной копии. Сделайте новую копию "
            "командой Резервная копия и отправьте файл снова",
        )
        return
    before = await journal.count(ctx.session)
    upload = JournalUpload(
        user_id=ctx.sender_id,
        user_fio=ctx.sender_fio,
        created_at=ctx.now,
        name=name,
        data=attachment.data,
        sha256=importer.fingerprint(attachment.data),
        rows=checked.rows,
        backup_id=backup.id,
        status="waiting",
    )
    ctx.session.add(upload)
    await ctx.session.flush()
    await pending.put(ctx, REPLACE_KIND, {"upload_id": upload.id, "rows": checked.rows})
    ctx.reply(
        "\n".join(
            [
                bold("Замена всего журнала КД"),
                f"Файл: {escape(name)}, записей: {checked.rows}. Сейчас в журнале: {before}.",
                bold("Весь журнал КД будет заменен записями из файла."),
                "Доступы, администраторы, журнал действий и остальные данные базы не меняются.",
                f"Резервная копия до замены: {ctx.local(backup.created_at)}, "
                f"{escape(backups.where(backup))}",
                "",
                markup.PROMPT,
            ]
        )
    )


async def answer_replace(ctx: Ctx, found: Pending, answer: bool) -> None:
    await pending.take(ctx, found)
    upload = await ctx.session.get(
        JournalUpload, int(found.payload["upload_id"]), with_for_update=True
    )
    if upload is None or upload.status != "waiting":
        refused_headed(ctx, "Этот файл уже обработан. Для новой замены отправьте файл заново")
        return
    if answer is False:
        upload.status = "canceled"
        upload.finished_at = ctx.now
        upload.reported_at = ctx.now
        ctx.reply(bold("Операция отменена. Журнал не изменен"))
        return
    upload.status = "confirmed"
    ctx.reply(
        bold("Замена журнала начата")
        + "\nПроверяю резервную копию и заменяю журнал. Итог придет отдельным сообщением."
    )
    ctx.later(_replace_task(ctx, upload.id))


def _replace_task(ctx: Ctx, upload_id: int) -> Task:
    """Заменить журнал. Итог — статус загрузки в базе, отправляет его `claim_reports`."""
    sessions, zone, clock = ctx.sessions, ctx.config.zone, ctx.clock
    user, fio = ctx.sender_id, ctx.sender_fio
    assert sessions is not None

    async def run() -> list[Reply]:
        try:
            await _replace(sessions, upload_id, zone=zone, clock=clock, user=user, fio=fio)
        except Exception:
            log.exception("Замена журнала %s не выполнена", upload_id)
            await importer.close_upload(sessions, upload_id, "refused", "внутренняя ошибка бота")
        return []

    return run


async def _replace(
    sessions: Any, upload_id: int, *, zone: Any, clock: Any, user: int, fio: str
) -> importer.Report:
    async with sessions() as session:
        upload = await session.get(JournalUpload, upload_id)
        backup = await session.get(BackupJob, upload.backup_id) if upload else None
    if upload is None or backup is None:
        return importer.Report(problems=["файл или резервная копия не найдены"])
    now: datetime = clock()
    if not importer.backup_fresh(backup, now):
        message = "резервная копия устарела (старше 30 минут) или не сделана"
        await importer.close_upload(sessions, upload_id, "refused", message)
        return importer.Report(problems=[message])
    checked = await backups.verify(sessions, backup, now)
    if checked.status != "done":
        message = f"резервная копия повреждена или недоступна: {checked.message}"
        await importer.close_upload(sessions, upload_id, "refused", message)
        return importer.Report(problems=[message])
    return await importer.replace_upload(
        sessions, upload_id, zone, clock, actor_id=user, actor_fio=fio
    )


def replace_report(upload: JournalUpload) -> str:
    if upload.status == "done":
        return (
            bold("Журнал КД заменен")
            + f"\n{escape(upload.message)}"
            + "\nДоступы, администраторы и журнал действий не менялись."
        )
    return (
        bold("Журнал не заменен, в базе все осталось как было")
        + "\n"
        + escape(upload.message)
        + "\nСделайте новую копию командой Резервная копия и отправьте файл снова."
    )


# ── Итоги ─────────────────────────────────────────────────────────────────────

REPORT_DAYS = timedelta(days=7)


@dataclass(frozen=True)
class Outgoing:
    """Итог для отправки: копия или замена журнала, кому и текст."""

    kind: str
    row_id: int
    user_id: int
    html: str


async def claim_reports(
    sessions: async_sessionmaker[AsyncSession],
    zone: ZoneInfo,
    now: datetime,
    owners: tuple[int, ...] = (),
) -> list[Outgoing]:
    """Забрать неотправленные итоги копий и замен журнала. Отметка «отправлено» ставится здесь
    же; если отправка не удалась, `unclaim` ее снимает, и итог уйдет в следующий раз."""
    found: list[Outgoing] = []
    async with sessions() as session, session.begin():
        jobs = await session.scalars(
            select(BackupJob)
            .where(
                BackupJob.kind == "backup",
                BackupJob.source != "terminal",
                BackupJob.status.in_(("done", "failed", "canceled")),
                BackupJob.reported_at.is_(None),
                BackupJob.created_at >= now - REPORT_DAYS,
            )
            .order_by(BackupJob.id)
            .with_for_update(skip_locked=True)
        )
        for job in jobs:
            job.reported_at = now
            session.add(
                AuditEntry(
                    at=now,
                    actor_id=job.requested_by,
                    actor_fio=job.requested_fio,
                    action="Резервная копия",
                    details=report_line(job),
                )
            )
            found.append(Outgoing("backup", job.id, job.requested_by, report(job, zone)))
            if job.source == "reminder":
                # Копию по напоминанию ждут все главные администраторы: итог коротко и им.
                state = "сделана" if job.successful else "не сделана"
                note = (
                    f"Резервная копия по напоминанию {state}. Делал: "
                    f"{escape(job.requested_fio or str(job.requested_by))}."
                )
                found += [
                    Outgoing("note", job.id, owner, note)
                    for owner in owners
                    if owner != job.requested_by
                ]
        uploads = await session.scalars(
            select(JournalUpload)
            .where(
                JournalUpload.status.in_(("done", "refused")),
                JournalUpload.reported_at.is_(None),
            )
            .order_by(JournalUpload.id)
            .with_for_update(skip_locked=True)
        )
        for upload in uploads:
            upload.reported_at = now
            found.append(Outgoing("upload", upload.id, upload.user_id, replace_report(upload)))
    return found


async def unclaim(sessions: async_sessionmaker[AsyncSession], item: Outgoing) -> None:
    if item.kind == "note":
        return  # короткая записка другим владельцам: повторять ее не нужно
    async with sessions() as session, session.begin():
        if item.kind == "backup":
            job = await session.get(BackupJob, item.row_id, with_for_update=True)
            if job is not None:
                job.reported_at = None
        else:
            upload = await session.get(JournalUpload, item.row_id, with_for_update=True)
            if upload is not None:
                upload.reported_at = None
