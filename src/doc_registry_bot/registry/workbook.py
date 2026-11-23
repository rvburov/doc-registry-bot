"""Чтение книги Excel или файла CSV со строками журнала.

Два случая:
- книга вложением в сообщении бота: ограничения по размеру, строкам и ячейкам, ФИО берется у
  отправителя; с разрешения вызывающего кода можно читать ФИО каждой строки. Столбец ФИО
  необязателен, пустая ячейка заменяется ФИО отправителя; «Дата регистрации» не читается;
- журнал целиком для замены на сервере (команда import, CSV или Excel): читаются все десять
  столбцов.

Лист — ПЕРВЫЙ, а не по имени: файл делают выгрузкой или вручную, и имя листа у каждого свое.
Номер строки — настоящий номер в книге: по нему человек ищет ошибку у себя в файле.
"""

import csv
import io
import os
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook

from doc_registry_bot.registry import schema
from doc_registry_bot.registry.text import same_name

EXTENSIONS = (".xlsx", ".xlsm")
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ROWS = 1000
MAX_CELLS = 100_000
_MAX_ENTRIES = 2048
_MAX_DEPTH = 64


class WorkbookError(Exception):
    """Книгу прочитать нельзя; текст годится для ответа человеку как есть."""


@dataclass(frozen=True)
class BookRow:
    """Строка книги: номер строки в книге и значения столбцов журнала (текстом, как в Excel)."""

    line: int
    name: str
    designation: str
    fio: str
    date: str
    project: str
    accounting: str
    first_use: str
    summary: str
    division: str = ""
    status: str = schema.REGISTERED


def safe_name(name: str | None) -> str:
    """Имя вложения: только последняя часть пути, без управляющих знаков."""
    value = os.path.basename((name or "").replace("\\", "/")).strip()
    if not value:
        raise WorkbookError("Файл: не указано имя вложения с расширением .xlsx или .xlsm.")
    if len(value) > 240 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise WorkbookError("Файл: недопустимое имя вложения.")
    return value


def read_message_book(
    data: bytes, file_name: str, sender_fio: str, *, allow_other_fio: bool = False
) -> list[BookRow]:
    """Книга из сообщения бота: проверка размера и устройства, затем строки первого листа."""
    name = safe_name(file_name)
    extension = os.path.splitext(name)[1].lower()
    if extension not in EXTENSIONS:
        raise WorkbookError("Файл: нужен формат .xlsx или .xlsm, старый .xls не поддерживается.")
    if not data:
        raise WorkbookError("Файл пустой.")
    if len(data) > MAX_FILE_BYTES:
        raise WorkbookError(f"Файл больше допустимых {MAX_FILE_BYTES // 1024 // 1024} МБ.")
    check_archive(data)
    return read_rows(
        data, max_rows=MAX_ROWS, sender_fio=sender_fio, allow_other_fio=allow_other_fio
    )


def check_archive(data: bytes) -> None:
    """Книга .xlsx — это zip: проверить число частей, распакованный объем и число ячеек.

    Проверка идет до разбора книги: сжатая «бомба» или лист на миллион ячеек не должны дойти
    до библиотеки чтения.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ENTRIES:
                raise WorkbookError("В книге слишком много внутренних частей.")
            names = [entry.filename for entry in entries]
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise WorkbookError("Файл не является книгой .xlsx или .xlsm.")
            if len(set(names)) != len(names):
                raise WorkbookError("В книге повторяются имена внутренних частей.")
            total = 0
            cells = 0
            for entry in entries:
                content = archive.read(entry)
                total += len(content)
                if total > MAX_EXPANDED_BYTES:
                    raise WorkbookError(
                        "Распакованная книга больше допустимых "
                        f"{MAX_EXPANDED_BYTES // 1024 // 1024} МБ."
                    )
                if entry.filename.startswith("xl/worksheets/") and entry.filename.endswith(".xml"):
                    cells += _count_cells(content)
                    if cells > MAX_CELLS:
                        raise WorkbookError(f"В книге больше допустимых {MAX_CELLS} ячеек.")
    except WorkbookError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, ValueError, OSError) as error:
        raise WorkbookError(
            "Не удалось прочитать книгу: файл поврежден или имеет неверный формат."
        ) from error


def _count_cells(content: bytes) -> int:
    if b"<!DOCTYPE" in content[:4096] or b"<!ENTITY" in content:
        raise WorkbookError("Не удалось прочитать книгу: файл поврежден или имеет неверный формат.")
    cells = 0
    depth = 0
    for event, element in ElementTree.iterparse(io.BytesIO(content), events=("start", "end")):
        if event == "start":
            depth += 1
            if depth > _MAX_DEPTH:
                raise WorkbookError("Структура листа книги слишком сложная.")
            if element.tag.rsplit("}", 1)[-1] == "c":
                cells += 1
                if cells > MAX_CELLS:
                    return cells
        else:
            depth -= 1
            element.clear()
    return cells


def read_rows(
    data: bytes,
    *,
    max_rows: int | None,
    sender_fio: str | None,
    allow_other_fio: bool = False,
) -> list[BookRow]:
    """Строки первого листа. При заданном `sender_fio` столбец ФИО необязателен;
    без `allow_other_fio` его значения не читаются."""
    try:
        book = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as error:
        raise WorkbookError(
            "Не удалось прочитать книгу: файл поврежден или имеет неверный формат."
        ) from error
    try:
        if not book.worksheets:
            raise WorkbookError("В книге нет ни одного листа.")
        sheet = book.worksheets[0]
        title = sheet.title
        # Только ячейки, которые есть в книге: строка 1 000 000 в разреженной книге не должна
        # превращаться в миллион пустых строк.
        by_row: dict[int, dict[int, str]] = {}
        stored: dict[tuple[int, int], Any] = sheet._cells
        for (row_number, column), stored_cell in stored.items():
            value = cell_text(stored_cell)
            if value:
                by_row.setdefault(row_number, {})[column] = value
    finally:
        book.close()
    return _book_rows(
        by_row,
        f"На листе «{title}»",
        max_rows=max_rows,
        sender_fio=sender_fio,
        allow_other_fio=allow_other_fio,
    )


def decode_text(data: bytes) -> str:
    """Текст файла: UTF-8 (с меткой или без), иначе Windows-1251 — так сохраняет CSV
    табличный редактор на русской Windows."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def table_cells(data: bytes) -> dict[int, dict[int, str]]:
    """Непустые ячейки CSV по строкам и столбцам (с единицы, как в книге).

    Разделитель — тот из «;», «,» и табуляции, которого больше всего в первой строке.
    """
    content = decode_text(data)
    first = next((line for line in content.splitlines() if line.strip()), "")
    delimiter = max(";,\t", key=first.count)
    cells: dict[int, dict[int, str]] = {}
    reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter)
    for row in reader:
        values = {
            column: value.strip() for column, value in enumerate(row, start=1) if value.strip()
        }
        if values:
            cells[reader.line_num] = values
    return cells


def read_csv(data: bytes, *, max_rows: int | None, sender_fio: str | None) -> list[BookRow]:
    """Строки CSV с шапкой журнала в первой непустой строке."""
    try:
        cells = table_cells(data)
    except (csv.Error, UnicodeDecodeError) as error:
        raise WorkbookError(f"Не удалось прочитать CSV: {error}.") from error
    return _book_rows(cells, "В файле", max_rows=max_rows, sender_fio=sender_fio)


def read_table(data: bytes, name: str, *, sender_fio: str | None = None) -> list[BookRow]:
    """Журнал из файла по расширению: CSV или книга Excel."""
    extension = os.path.splitext(name)[1].lower()
    if extension == ".csv":
        return read_csv(data, max_rows=None, sender_fio=sender_fio)
    if extension in EXTENSIONS:
        return read_rows(data, max_rows=None, sender_fio=sender_fio)
    raise WorkbookError(f"Нужен файл .csv, .xlsx или .xlsm, а указан «{extension or name}».")


def _book_rows(
    by_row: dict[int, dict[int, str]],
    where: str,
    *,
    max_rows: int | None,
    sender_fio: str | None,
    allow_other_fio: bool = False,
) -> list[BookRow]:
    if not by_row:
        raise WorkbookError(f"{where} нет данных.")
    numbers = sorted(by_row)
    header_row = by_row[numbers[0]]
    width = max(header_row)
    header = [header_row.get(column, "") for column in range(1, width + 1)]
    at = _header(header, where, from_sender=sender_fio is not None, allow_other_fio=allow_other_fio)

    result: list[BookRow] = []
    for number in numbers[1:]:
        if max_rows is not None and len(result) >= max_rows:
            raise WorkbookError(
                f"В одном файле допускается не больше {max_rows} строк с документами."
            )
        values = by_row[number]

        def cell(index: int, row: dict[int, str] = values) -> str:
            return row.get(index + 1, "") if index >= 0 else ""

        def field(name: str) -> str:
            return cell(at[schema.COLUMNS.index(name)])

        fio = field(schema.FIO)
        if sender_fio is not None and (not allow_other_fio or not fio):
            fio = sender_fio
        result.append(
            BookRow(
                line=number,
                name=field(schema.TITLE),
                designation=field(schema.DESIGNATION),
                fio=fio,
                date=field(schema.DATE),
                project=field(schema.PROJECT),
                accounting=field(schema.ACCOUNTING),
                first_use=field(schema.FIRST_USE),
                summary=field(schema.SUMMARY),
                division=field(schema.DIVISION),
                status=field(schema.STATUS)
                if at[schema.COLUMNS.index(schema.STATUS)] >= 0
                else schema.REGISTERED,
            )
        )
    if not result:
        raise WorkbookError(f"{where} только заголовок, строк с номерами нет.")
    return result


def _header(
    header: list[str], where: str, *, from_sender: bool, allow_other_fio: bool = False
) -> list[int]:
    """Номера столбцов журнала в шапке (с нуля) в порядке журнала."""
    cells = [(name, index) for index, name in enumerate(header)]
    if from_sender:
        if not allow_other_fio:
            # ФИО заполняет карточка отправителя: старый столбец, если остался, не читается.
            cells = [cell for cell in cells if not same_name(cell[0], schema.FIO)]
        if not any(same_name(cell[0], schema.FIO) for cell in cells):
            cells.append((schema.FIO, -1))
    for extra in (schema.DIVISION, schema.STATUS):
        if not any(same_name(cell[0], extra) for cell in cells):
            cells.append((extra, -1))
    found, problem = schema.find(cells, lambda cell: cell[0])
    if found is None:
        raise WorkbookError(f"{where} {problem}.")
    return [index for _, index in found]


def cell_text(cell: Any) -> str:
    """Текст ячейки так, как его видит человек в Excel (для обычных форматов)."""
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ИСТИНА" if value else "ЛОЖЬ"
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.strftime("%d.%m.%Y")
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.15g}".replace(".", ",")
    return str(value).strip()
