"""Книги Excel для тестов: шапка журнала и строки по номерам строк листа."""

import io
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from openpyxl import Workbook

from doc_registry_bot.registry import schema


@dataclass(frozen=True)
class Row:
    line: int
    number: str = "АБВГ.301121.ХХХ"
    name: str = "Документ"
    fio: str = "Иванов Иван Иванович"
    project: str = "Проект"
    accounting: str = ""
    first_use: str = ""
    summary: str = ""
    division: str = ""
    status: str = schema.REGISTERED


def fill(sheet: Any, row: Row) -> None:
    values = dict(
        zip(
            schema.COLUMNS,
            [
                row.name,
                row.number,
                row.project,
                "дата из файла не используется",
                row.fio,
                row.division,
                row.status,
                row.accounting,
                row.first_use,
                row.summary,
            ],
            strict=True,
        )
    )
    for index in range(1, sheet.max_column + 1):
        value = values.get(schema.field_name(str(sheet.cell(1, index).value)), "")
        if value:
            sheet.cell(row=row.line, column=index, value=value)


def create_sheet(fill_sheet: Callable[[Any], None], header: Sequence[str] | None = None) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Журнал"
    for index, name in enumerate(header if header is not None else schema.COLUMNS, start=1):
        sheet.cell(row=1, column=index, value=name)
    fill_sheet(sheet)
    data = io.BytesIO()
    book.save(data)
    return data.getvalue()


def create(*rows: Row) -> bytes:
    def fill_all(sheet: Any) -> None:
        for row in rows:
            fill(sheet, row)

    return create_sheet(fill_all)


def archive(*entries: tuple[str, str]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zip_file:
        for name, text in entries:
            zip_file.writestr(name, text)
    return data.getvalue()
