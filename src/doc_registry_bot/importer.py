"""Перенос журнала из выгрузки SmartBase (книга Excel) в пустую базу бота. Один раз, при переезде.

Читаются восемь столбцов журнала по именам, лишние (свои столбцы SmartBase) пропускаются.
Даты и ФИО сохраняются как в журнале. Загрузка идет целиком или никак: одна ошибка — в базе
не появляется ни одной строки, а отчет называет все ошибки сразу.
"""

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.db import journal
from doc_registry_bot.db.models import JournalEntry
from doc_registry_bot.registry import workbook

_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


@dataclass
class ImportReport:
    rows: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


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


async def import_journal(
    sessions: async_sessionmaker[AsyncSession], data: bytes, zone: ZoneInfo, now: datetime
) -> ImportReport:
    report = ImportReport()
    try:
        rows = workbook.read_rows(data, max_rows=None, sender_fio=None)
    except workbook.WorkbookError as error:
        report.problems.append(str(error))
        return report
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
    if report.problems:
        return report
    async with sessions() as session, session.begin():
        if await journal.count(session):
            report.problems.append(
                "В базе бота журнал уже не пустой. Загрузка делается один раз, в пустую базу."
            )
            return report
        session.add_all(entries)
    report.rows = len(entries)
    return report
