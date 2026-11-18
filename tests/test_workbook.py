"""Книга Excel вложением."""

import csv
import io
import zipfile
from typing import Any

import pytest

from doc_registry_bot.registry import schema, workbook
from doc_registry_bot.registry.workbook import WorkbookError
from tests import books

SENDER = "Иванов Иван Иванович"


@pytest.mark.parametrize("name", ["Список.xlsx", "Список.XLSX", "Список.xlsm"])
def test_the_template_keeps_fields_and_real_sheet_row_numbers(name: str) -> None:
    data = books.create(
        books.Row(2, name="Втулка", accounting="Учтен", first_use="Изделие", summary="Сталь"),
        books.Row(7, name="Вал", fio="Петров Петр Петрович", project="Второй проект"),
    )
    rows = workbook.read_message_book(data, name, SENDER)
    assert [row.line for row in rows] == [2, 7]
    assert rows[0].name == "Втулка"
    assert rows[0].designation == "АБВГ.301121.ХХХ"
    assert rows[0].accounting == "Учтен"
    assert rows[0].first_use == "Изделие"
    assert rows[0].summary == "Сталь"
    assert rows[1].project == "Второй проект"


def test_the_fio_comes_from_the_sender_not_from_the_book() -> None:
    data = books.create(books.Row(2, fio="Петров Петр Петрович"))
    [row] = workbook.read_message_book(data, "Список.xlsx", SENDER)
    assert row.fio == SENDER


def test_an_allowed_message_book_keeps_the_fio_of_each_document() -> None:
    data = books.create(
        books.Row(2, fio="Петров Петр Петрович"),
        books.Row(5, fio="Сидоров Сергей Сергеевич"),
        # Полноту ФИО проверяет разбор заявки, чтение книги должно передать его без замены.
        books.Row(7, fio="Неполное"),
    )
    rows = workbook.read_message_book(data, "Список.xlsx", SENDER, allow_other_fio=True)
    assert [row.fio for row in rows] == [
        "Петров Петр Петрович",
        "Сидоров Сергей Сергеевич",
        "Неполное",
    ]
    assert [row.line for row in rows] == [2, 5, 7]


def test_a_default_message_book_ignores_all_fio_values() -> None:
    data = books.create(
        books.Row(2, fio="Петров Петр Петрович"),
        books.Row(3, fio="Неполное"),
        books.Row(4, fio=""),
    )
    rows = workbook.read_message_book(data, "Список.xlsx", SENDER)
    assert [row.fio for row in rows] == [SENDER, SENDER, SENDER]


def test_blank_fio_cells_use_the_sender_in_an_allowed_message_book() -> None:
    data = books.create(books.Row(2, fio=""), books.Row(3, fio=" \t "))
    rows = workbook.read_message_book(data, "Список.xlsx", SENDER, allow_other_fio=True)
    assert [row.fio for row in rows] == [SENDER, SENDER]


@pytest.mark.parametrize("allow_other_fio", [False, True])
def test_the_fio_column_is_not_required_in_a_message_book(allow_other_fio: bool) -> None:
    header = [column for column in schema.COLUMNS if column != schema.FIO]

    def fill(sheet: Any) -> None:
        values = ["Втулка", "АБВГ.301121.ХХХ", "", "Машина", "", "", ""]
        for index, value in enumerate(values, start=1):
            if value:
                sheet.cell(row=2, column=index, value=value)

    [row] = workbook.read_message_book(
        books.create_sheet(fill, header), "Список.xlsx", SENDER, allow_other_fio=allow_other_fio
    )
    assert row.name == "Втулка"
    assert row.designation == "АБВГ.301121.ХХХ"
    assert row.project == "Машина"
    assert row.fio == SENDER


def test_columns_are_resolved_by_name_and_extra_columns_are_skipped() -> None:
    header = [*reversed(schema.COLUMNS), "Своя колонка"]

    def fill(sheet: Any) -> None:
        values = [
            "Кратко",
            "Применение",
            "Учтен",
            "Машина",
            "не дата",
            SENDER,
            "АБВГ.301121.ХХХ",
            "Втулка",
            "Лишнее",
        ]
        for index, value in enumerate(values, start=1):
            sheet.cell(row=2, column=index, value=value)

    [row] = workbook.read_message_book(books.create_sheet(fill, header), "Список.xlsx", SENDER)
    assert row.name == "Втулка"
    assert row.designation == "АБВГ.301121.ХХХ"
    assert row.project == "Машина"
    assert row.first_use == "Применение"
    assert row.summary == "Кратко"


def test_an_allowed_message_book_reads_the_fio_column_in_any_position() -> None:
    header = ["Своя колонка", *reversed(schema.COLUMNS)]

    def fill(sheet: Any) -> None:
        values = {
            schema.TITLE: "Втулка",
            schema.DESIGNATION: "АБВГ.301121.ХХХ",
            schema.FIO: "Петров Петр Петрович",
            schema.PROJECT: "Машина",
            schema.ACCOUNTING: "Учтен",
            schema.SUMMARY: "Сталь",
        }
        for index, name in enumerate(header, start=1):
            sheet.cell(row=4, column=index, value=values.get(name, ""))

    [row] = workbook.read_message_book(
        books.create_sheet(fill, header), "Список.xlsx", SENDER, allow_other_fio=True
    )
    assert row.fio == "Петров Петр Петрович"
    assert row.line == 4
    assert row.name == "Втулка"
    assert row.designation == "АБВГ.301121.ХХХ"
    assert row.project == "Машина"
    assert row.accounting == "Учтен"
    assert row.summary == "Сталь"


def test_duplicate_fio_headers_are_refused_when_the_column_is_read() -> None:
    data = books.create_sheet(
        lambda sheet: books.fill(sheet, books.Row(2)), [*schema.COLUMNS, schema.FIO]
    )
    with pytest.raises(WorkbookError, match=r"ФИО регистрирующего.*несколько раз"):
        workbook.read_message_book(data, "Список.xlsx", SENDER, allow_other_fio=True)
    # Обычная регистрация по-прежнему полностью игнорирует старые столбцы ФИО.
    [row] = workbook.read_message_book(data, "Список.xlsx", SENDER)
    assert row.fio == SENDER


def test_sparse_books_keep_far_sheet_lines_without_treating_blank_rows_as_documents() -> None:
    data = books.create(books.Row(2, name="Первый документ"), books.Row(1_000_000, name="Далекий"))
    rows = workbook.read_message_book(data, "Разреженный список.xlsx", SENDER)
    assert [row.line for row in rows] == [2, 1_000_000]
    assert [row.name for row in rows] == ["Первый документ", "Далекий"]


def test_a_missing_or_duplicate_header_refuses_the_whole_book() -> None:
    def one(sheet: Any) -> None:
        books.fill(sheet, books.Row(2))

    missing = books.create_sheet(one, schema.COLUMNS[:7])
    duplicate = books.create_sheet(one, [*schema.COLUMNS, schema.PROJECT])
    with pytest.raises(WorkbookError, match=r"Примечание \(краткая характеристика\)"):
        workbook.read_rows(missing, max_rows=None, sender_fio=None)
    with pytest.raises(WorkbookError, match="Проект"):
        workbook.read_message_book(duplicate, "Повтор.xlsx", SENDER)


@pytest.mark.parametrize("name", ["Список.xls", "Список.csv", "Список.zip", ""])
def test_unsupported_or_missing_names_are_refused_before_reading(name: str) -> None:
    with pytest.raises(WorkbookError):
        workbook.read_message_book(books.create(books.Row(2)), name, SENDER)


def test_empty_or_damaged_input_does_not_reach_the_workbook_reader() -> None:
    with pytest.raises(WorkbookError):
        workbook.read_message_book(b"", "Пустой.xlsx", SENDER)
    with pytest.raises(WorkbookError):
        workbook.read_message_book(bytes([1, 2, 3, 4]), "Поврежден.xlsx", SENDER)
    with pytest.raises(WorkbookError):
        workbook.read_message_book(
            books.archive(("other.txt", "это не книга")), "Не книга.xlsx", SENDER
        )
    with pytest.raises(WorkbookError):
        workbook.read_message_book(books.create_sheet(lambda _: None), "Только шапка.xlsx", SENDER)


def test_the_compressed_file_size_is_limited_before_zip_parsing() -> None:
    with pytest.raises(WorkbookError, match="МБ"):
        workbook.read_message_book(bytes(workbook.MAX_FILE_BYTES + 1), "Большой.xlsx", SENDER)


def test_a_large_expanded_part_is_refused_even_when_the_archive_is_small() -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "")
        archive.writestr("xl/workbook.xml", "")
        archive.writestr("padding.bin", bytes(workbook.MAX_EXPANDED_BYTES + 1))
    assert len(data.getvalue()) < workbook.MAX_FILE_BYTES
    with pytest.raises(WorkbookError, match="Распакованная"):
        workbook.read_message_book(data.getvalue(), "Сжатый.xlsx", SENDER)


def test_a_large_sheet_is_refused_before_workbook_loading() -> None:
    cells = "<c/>" * (workbook.MAX_CELLS + 1)
    data = books.archive(
        ("[Content_Types].xml", ""),
        ("xl/workbook.xml", ""),
        (
            "xl/worksheets/sheet1.xml",
            f"<worksheet><sheetData><row>{cells}</row></sheetData></worksheet>",
        ),
    )
    with pytest.raises(WorkbookError, match="ячеек"):
        workbook.read_message_book(data, "Много ячеек.xlsx", SENDER)


def test_duplicate_internal_parts_and_entities_are_refused() -> None:
    with pytest.warns(UserWarning):
        duplicate = books.archive(
            ("[Content_Types].xml", ""), ("xl/workbook.xml", ""), ("xl/workbook.xml", "повтор")
        )
    entity = books.archive(
        ("[Content_Types].xml", ""),
        ("xl/workbook.xml", ""),
        (
            "xl/worksheets/sheet1.xml",
            "<!DOCTYPE worksheet [<!ENTITY item 'значение'>]><worksheet>&item;</worksheet>",
        ),
    )
    with pytest.raises(WorkbookError, match="повторяются"):
        workbook.read_message_book(duplicate, "Повтор частей.xlsx", SENDER)
    with pytest.raises(WorkbookError):
        workbook.read_message_book(entity, "Сущности.xlsx", SENDER)


def test_a_file_with_too_many_data_rows_is_refused_instead_of_partly_imported() -> None:
    rows = [books.Row(line) for line in range(2, workbook.MAX_ROWS + 3)]
    with pytest.raises(WorkbookError, match=str(workbook.MAX_ROWS)):
        workbook.read_message_book(books.create(*rows), "Много строк.xlsx", SENDER)


def test_a_journal_export_keeps_every_column_for_import() -> None:
    data = books.create(books.Row(2, name="Вал", fio="Петров Петр Петрович", project="Станок"))
    [row] = workbook.read_rows(data, max_rows=None, sender_fio=None)
    assert row.fio == "Петров Петр Петрович"
    assert row.date == "дата из файла не используется"


def journal_table(extension: str, header: list[str], values: list[list[str]]) -> bytes:
    """Одинаковая таблица для проверки строгого импорта CSV и книги."""
    if extension == "csv":
        text = io.StringIO(newline="")
        writer = csv.writer(text, delimiter=";")
        writer.writerow(header)
        writer.writerows(values)
        return text.getvalue().encode("utf-8-sig")

    def fill(sheet: Any) -> None:
        for number, row in enumerate(values, start=2):
            for column, value in enumerate(row, start=1):
                sheet.cell(row=number, column=column, value=value)

    return books.create_sheet(fill, header)


@pytest.mark.parametrize("extension", ["xlsx", "csv"])
def test_journal_import_still_requires_the_fio_column(extension: str) -> None:
    header = [column for column in schema.COLUMNS if column != schema.FIO]
    data = journal_table(
        extension, header, [["Втулка", "АБВГ.301121.001", "10.11.2026", "Станок", "", "", ""]]
    )
    with pytest.raises(WorkbookError, match="нет столбца «ФИО регистрирующего»"):
        workbook.read_table(data, f"Журнал.{extension}")


@pytest.mark.parametrize("extension", ["xlsx", "csv"])
def test_journal_import_keeps_fio_cells_and_dates_as_written(extension: str) -> None:
    values = [
        ["Втулка", "АБВГ.301121.001", "Петров Петр Петрович", "10.11.2026", "Станок", "", "", ""],
        ["Вал", "АБВГ.301121.002", "", "11.11.2026", "Станок", "", "", ""],
    ]
    data = journal_table(extension, [*schema.COLUMNS], values)
    rows = workbook.read_table(data, f"Журнал.{extension}")
    assert [row.fio for row in rows] == ["Петров Петр Петрович", ""]
    assert [row.date for row in rows] == ["10.11.2026", "11.11.2026"]


@pytest.mark.parametrize("extension", ["xlsx", "csv"])
def test_journal_import_still_refuses_duplicate_fio_headers(extension: str) -> None:
    data = journal_table(
        extension,
        [*schema.COLUMNS, schema.FIO],
        [["Втулка", "АБВГ.301121.001", SENDER, "10.11.2026", "Станок", "", "", "", SENDER]],
    )
    with pytest.raises(WorkbookError, match=r"ФИО регистрирующего.*несколько раз"):
        workbook.read_table(data, f"Журнал.{extension}")
