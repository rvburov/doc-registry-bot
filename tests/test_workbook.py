"""Книга Excel вложением. Примеры перенесены из FileMessageReadTests SmartBase."""

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


def test_the_fio_column_is_not_required_in_a_message_book() -> None:
    header = [column for column in schema.COLUMNS if column != schema.FIO]

    def fill(sheet: Any) -> None:
        values = ["Втулка", "АБВГ.301121.ХХХ", "", "Машина", "", "", ""]
        for index, value in enumerate(values, start=1):
            if value:
                sheet.cell(row=2, column=index, value=value)

    [row] = workbook.read_message_book(books.create_sheet(fill, header), "Список.xlsx", SENDER)
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
