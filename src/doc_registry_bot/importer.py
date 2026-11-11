"""Журнал и пользователи из файлов и в файлы: команды на сервере (deploy/backup.sh, import.sh,
users.sh).

Замена журнала опасна: старые записи удаляются. Поэтому она выполняется только после резерва:
`backup.sh` выгружает журнал в CSV, отпечаток этой выгрузки (SHA-256) передается замене, и
замена сверяет его с журналом в базе в той же транзакции. Журнал изменился после резерва
(кто-то зарегистрировал номер) или резерва не было — замена отказывает и ничего не трогает.
"""

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.db import journal
from doc_registry_bot.db.models import AuditEntry, JournalEntry, Member
from doc_registry_bot.registry import schema, workbook
from doc_registry_bot.registry.text import collapse, same_name

_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")

# Кто выполнил команду на сервере — в журнале действий.
SERVER_ACTOR = "администратор сервера"


@dataclass
class Report:
    rows: int = 0
    before: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ── Выгрузка ──────────────────────────────────────────────────────────────────


def csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    """CSV для табличного редактора: UTF-8 с меткой и разделитель «;»."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _local(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M")


async def export_csv(session: AsyncSession, zone: ZoneInfo) -> bytes:
    """Весь журнал КД: восемь столбцов, по порядку записи. Одни и те же данные — одни и те же
    байты: на этом держится отпечаток резерва."""
    entries = await journal.all_entries(session)
    return csv_bytes(
        list(schema.COLUMNS),
        [
            [
                e.name,
                e.designation,
                e.fio,
                _local(e.registered_at, zone),
                e.project,
                e.accounting,
                e.first_use,
                e.summary,
            ]
            for e in entries
        ],
    )


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Замена журнала ────────────────────────────────────────────────────────────


def parse_date(value: str, zone: ZoneInfo) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
        for pattern in _FORMATS:
            try:
                moment = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def _entries(
    rows: list[workbook.BookRow], zone: ZoneInfo, now: datetime, report: Report
) -> list[JournalEntry]:
    seen: dict[str, int] = {}
    entries: list[JournalEntry] = []
    for row in rows:
        where = f"Строка {row.line}"
        if not row.designation:
            report.problems.append(f"{where}: пустое «Обозначение»")
            continue
        key = journal.designation_key(row.designation)
        if key in seen:
            report.problems.append(f"{where}: «{row.designation}» уже есть в строке {seen[key]}")
            continue
        seen[key] = row.line
        moment = parse_date(row.date, zone)
        if moment is None:
            if row.date:
                report.problems.append(f"{where}: не разобрана дата «{row.date}»")
                continue
            moment = now
        entries.append(
            JournalEntry(
                designation=row.designation.strip(),
                designation_key=key,
                series=journal.series_of(row.designation),
                name=row.name,
                fio=row.fio,
                registered_at=moment,
                project=row.project,
                accounting=row.accounting,
                first_use=row.first_use,
                summary=row.summary,
                updated_at=now,
            )
        )
    return entries


def read_journal(
    data: bytes, name: str, zone: ZoneInfo, now: datetime
) -> tuple[list[JournalEntry], Report]:
    """Проверить файл: строки для записи и все ошибки сразу."""
    report = Report()
    try:
        rows = workbook.read_table(data, name)
    except workbook.WorkbookError as error:
        report.problems.append(str(error))
        return [], report
    entries = _entries(rows, zone, now, report)
    report.rows = len(entries)
    return entries, report


async def replace_journal(
    sessions: async_sessionmaker[AsyncSession],
    data: bytes,
    name: str,
    zone: ZoneInfo,
    now: datetime,
    *,
    expected: str | None,
) -> Report:
    """Заменить весь журнал строками файла. Только после резерва с отпечатком `expected`."""
    entries, report = read_journal(data, name, zone, now)
    if not expected:
        report.problems.append(
            "Замена без резерва невозможна. Сначала сделайте резерв: ./backup.sh"
        )
    if report.problems:
        return report
    async with sessions() as session, session.begin():
        # Регистрация в боте ждет, пока замена не закончится: журнал меняется целиком.
        await session.execute(sql("LOCK TABLE journal IN EXCLUSIVE MODE"))
        current = fingerprint(await export_csv(session, zone))
        if current != expected:
            report.problems.append(
                "Журнал изменился после резерва (или резерв от другой базы). "
                "Сделайте резерв заново: ./backup.sh"
            )
            return report
        report.before = await journal.count(session)
        await session.execute(delete(JournalEntry))
        session.add_all(entries)
        session.add(
            AuditEntry(
                at=now,
                actor_id=0,
                actor_fio=SERVER_ACTOR,
                action="Замена журнала из файла",
                details=f"файл: {name}, было записей: {report.before}, стало: {len(entries)}",
            )
        )
    return report


# ── Пользователи ──────────────────────────────────────────────────────────────


async def load_users(
    sessions: async_sessionmaker[AsyncSession], data: bytes, name: str, now: datetime
) -> Report:
    """Выдать доступ всем из файла (столбцы «ID» и «ФИО»), без инструкции. Целиком или никак."""
    report = Report()
    if not name.lower().endswith(".csv"):
        report.problems.append("Нужен файл .csv со столбцами ID и ФИО")
        return report
    try:
        cells = workbook.table_cells(data)
    except (csv.Error, UnicodeDecodeError) as error:
        report.problems.append(f"Не удалось прочитать CSV: {error}")
        return report
    if not cells:
        report.problems.append("Файл пустой")
        return report
    numbers = sorted(cells)
    header = cells[numbers[0]]
    id_column = next((c for c, v in header.items() if same_name(v, "ID")), None)
    fio_column = next((c for c, v in header.items() if same_name(v, "ФИО")), None)
    if id_column is None or fio_column is None:
        report.problems.append("В первой строке нужны столбцы ID и ФИО")
        return report
    people: dict[int, str] = {}
    for line in numbers[1:]:
        value = cells[line].get(id_column, "")
        fio = collapse(cells[line].get(fio_column, ""))
        if not value.isdigit() or int(value) <= 0:
            report.problems.append(f"Строка {line}: «{value}» не числовой ID")
            continue
        if not fio:
            report.problems.append(f"Строка {line}: не указано ФИО")
            continue
        if int(value) in people:
            report.problems.append(f"Строка {line}: ID {value} уже есть выше")
            continue
        people[int(value)] = fio
    if not people and not report.problems:
        report.problems.append("В файле нет строк с пользователями")
    if report.problems:
        return report
    async with sessions() as session, session.begin():
        report.before = await session.scalar(select(func.count()).select_from(Member)) or 0
        for user_id, fio in people.items():
            member = await session.get(Member, user_id, with_for_update=True)
            if member is None:
                session.add(Member(user_id=user_id, fio=fio, has_access=True, is_admin=False))
            else:
                member.has_access = True
                member.fio = fio
                member.updated_at = now
        session.add(
            AuditEntry(
                at=now,
                actor_id=0,
                actor_fio=SERVER_ACTOR,
                action="Выдача доступа из файла",
                details=f"файл: {name}, пользователей: {len(people)}",
            )
        )
    report.rows = len(people)
    return report
