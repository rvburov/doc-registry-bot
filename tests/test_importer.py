"""Команды на сервере: выгрузка журнала, замена журнала из файла только после резерва,
выдача доступа из файла."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot import importer
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import UserCard
from doc_registry_bot.registry import schema
from tests import books
from tests.stand import ADMIN, REGISTRAR, Stand

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
HEADER = ";".join(schema.COLUMNS)


def csv_file(*lines: str, encoding: str = "utf-8-sig") -> bytes:
    return "\r\n".join([";".join(schema.LEGACY_COLUMNS), *lines]).encode(encoding)


async def backup(stand: Stand, config: BotConfig) -> str:
    """То, что делает ./backup.sh: выгрузка журнала и ее отпечаток."""
    async with stand.db() as session:
        return importer.fingerprint(await importer.export_csv(session, config.zone))


async def replace(
    stand: Stand, config: BotConfig, data: bytes, name: str, expected: str | None
) -> importer.Report:
    return await importer.replace_journal(stand.db, data, name, config.zone, NOW, expected=expected)


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    return result


# ── Выгрузка ──────────────────────────────────────────────────────────────────


async def test_an_empty_journal_is_exported_with_the_header(
    stand: Stand, config: BotConfig
) -> None:
    async with stand.db() as session:
        data = await importer.export_csv(session, config.zone)
    assert data == ("﻿" + HEADER + "\r\n").encode()


async def test_the_export_is_the_same_bytes_for_the_same_journal(
    stand: Stand, config: BotConfig
) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    first = await backup(stand, config)
    assert await backup(stand, config) == first
    await stand.add("АБВГ.301121.003")
    assert await backup(stand, config) != first


# ── Замена журнала ────────────────────────────────────────────────────────────


async def test_the_journal_is_not_replaced_without_a_backup(
    stand: Stand, config: BotConfig
) -> None:
    await stand.add("АБВГ.301121.001")
    report = await replace(stand, config, csv_file("Вал;АБВГ.301121.005"), "журнал.csv", None)
    assert report.problems == [
        "Замена без резерва невозможна. Сначала сделайте резерв: ./backup.sh"
    ]
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_a_number_registered_after_the_backup_stops_the_replacement(
    stand: Stand, config: BotConfig
) -> None:
    await stand.add("АБВГ.301121.001")
    expected = await backup(stand, config)
    await stand.add("АБВГ.301121.002")
    report = await replace(stand, config, csv_file("Вал;АБВГ.301121.005"), "журнал.csv", expected)
    assert report.problems == [
        "Журнал изменился после резерва (или резерв от другой базы). "
        "Сделайте резерв заново: ./backup.sh"
    ]
    assert await stand.designations() == ["АБВГ.301121.001", "АБВГ.301121.002"]


async def test_the_journal_is_replaced_from_csv_and_access_stays(
    stand: Stand, config: BotConfig
) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    expected = await backup(stand, config)
    data = csv_file(
        "Вал;АБВГ.301121.010;Иванов Иван Иванович;01.02.2024 10:30;Машина;;;",
        "Ось;АБВГ.301121.011-01;Петров Петр Петрович;;Станок;учтен;;",
    )
    report = await replace(stand, config, data, "журнал.csv", expected)
    assert report.ok, report.problems
    assert (report.before, report.rows) == (2, 2)
    entries = await stand.entries()
    assert [e.designation for e in entries] == ["АБВГ.301121.010", "АБВГ.301121.011-01"]
    assert entries[0].registered_at == datetime(2024, 2, 1, 7, 30, tzinfo=UTC)
    assert entries[1].registered_at == NOW
    assert entries[1].accounting == "учтен"
    assert entries[1].series == "АБВГ.301121."
    member = await stand.member(ADMIN)
    assert member is not None and member.is_admin and member.has_access
    [audit] = [a for a in await stand.audit() if a.action == "Замена журнала из файла"]
    assert audit.actor_fio == importer.SERVER_ACTOR
    assert audit.details == "файл: журнал.csv, было записей: 2, стало: 2"


async def test_the_backup_file_restores_the_journal_as_it_was(
    stand: Stand, config: BotConfig
) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002-01")
    async with stand.db() as session:
        saved = await importer.export_csv(session, config.zone)
    expected = importer.fingerprint(saved)
    report = await replace(stand, config, csv_file("Вал;АБВГ.301121.005"), "новый.csv", expected)
    assert report.ok, report.problems
    expected = await backup(stand, config)
    report = await replace(stand, config, saved, "журнал-резерв.csv", expected)
    assert report.ok, report.problems
    async with stand.db() as session:
        assert await importer.export_csv(session, config.zone) == saved


async def test_an_empty_journal_can_be_filled_after_its_backup(
    stand: Stand, config: BotConfig
) -> None:
    expected = await backup(stand, config)
    data = csv_file("Вал;АБВГ.301121.001", "Ось;АБВГ.301121.002")
    report = await replace(stand, config, data, "журнал.csv", expected)
    assert report.ok, report.problems
    assert report.before == 0
    assert await stand.designations() == ["АБВГ.301121.001", "АБВГ.301121.002"]


async def test_an_excel_export_with_extra_columns_is_loaded(
    stand: Stand, config: BotConfig
) -> None:
    def fill(sheet: object) -> None:
        rows = [
            ["Вал", "АБВГ.301121.001", "Иванов Иван Иванович", "01.02.2024 10:30", "Машина"],
            ["Ось", "АБВГ.301121.002-01", "Петров Петр Петрович", "", "Станок"],
        ]
        for line, values in enumerate(rows, start=2):
            for column, value in enumerate(values, start=1):
                if value:
                    sheet.cell(row=line, column=column, value=value)  # type: ignore[attr-defined]

    data = books.create_sheet(fill, [*books.schema.LEGACY_COLUMNS, "Письмо"])
    report = await replace(stand, config, data, "журнал.xlsx", await backup(stand, config))
    assert report.ok, report.problems
    assert report.rows == 2


async def test_errors_in_the_file_stop_the_whole_replacement(
    stand: Stand, config: BotConfig
) -> None:
    await stand.add("АБВГ.301121.001")
    data = csv_file(
        "Вал;АБВГ.301121.005",
        "Ось;абвг.301121.005",
        "Втулка;;",
        "Шайба;АБВГ.301121.006;;31.02.2024",
    )
    report = await replace(stand, config, data, "журнал.csv", await backup(stand, config))
    assert report.problems == [
        "Строка 3: «абвг.301121.005» уже есть в строке 2",
        "Строка 4: пустое «Обозначение»",
        "Строка 5: не разобрана дата «31.02.2024»",
    ]
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_a_csv_from_a_russian_spreadsheet_is_read(stand: Stand, config: BotConfig) -> None:
    lines = [",".join(schema.LEGACY_COLUMNS), '"Вал, полый",АБВГ.301121.001,,,Машина,,,']
    data = "\r\n".join(lines).encode("cp1251")
    entries, report = importer.read_journal(data, "журнал.CSV", config.zone, NOW)
    assert report.ok, report.problems
    assert [(e.name, e.designation, e.project) for e in entries] == [
        ("Вал, полый", "АБВГ.301121.001", "Машина")
    ]


async def test_a_file_without_the_journal_header_is_refused(
    stand: Stand, config: BotConfig
) -> None:
    _, report = importer.read_journal(b"a;b\r\n1;2", "x.csv", config.zone, NOW)
    assert not report.ok
    assert report.problems[0].startswith("В файле нет столбцов: «Наименование»")
    _, report = importer.read_journal(b"", "x.txt", config.zone, NOW)
    assert report.problems == ["Нужен файл .csv, .xlsx или .xlsm, а указан «.txt»."]


# ── Пользователи ──────────────────────────────────────────────────────────────


async def test_users_get_access_from_a_file_and_admins_stay_admins(stand: Stand) -> None:
    data = "ID;ФИО\r\n5001;Сидоров Сидор Сидорович\r\n2;Админов Андрей Андреевич\r\n".encode()
    report = await importer.load_users(stand.db, data, "пользователи.csv", NOW)
    assert report.ok, report.problems
    assert report.rows == 2
    new = await stand.member(5001)
    assert new is not None and new.has_access and not new.is_admin
    assert new.fio == "Сидоров Сидор Сидорович"
    admin = await stand.member(ADMIN)
    assert admin is not None and admin.is_admin
    [audit] = [a for a in await stand.audit() if a.action == "Выдача доступа из файла"]
    assert audit.details == "файл: пользователи.csv, пользователей: 2"
    assert not stand.messenger.sent


async def test_a_bad_id_stops_the_whole_user_file(stand: Stand) -> None:
    data = "ФИО,ID\r\nСидоров Сидор Сидорович,5001\r\nПетров Петр,петров\r\n".encode("cp1251")
    report = await importer.load_users(stand.db, data, "пользователи.csv", NOW)
    assert report.problems == ["Строка 3: «петров» не числовой ID"]
    assert await stand.member(5001) is None


async def test_an_empty_fio_or_a_repeated_id_stops_the_user_file(stand: Stand) -> None:
    data = "ID;ФИО\r\n5001;\r\n5002;Петров Петр Петрович\r\n5002;Петров П. П.\r\n".encode()
    report = await importer.load_users(stand.db, data, "пользователи.csv", NOW)
    assert report.problems == ["Строка 2: не указано ФИО", "Строка 4: ID 5002 уже есть выше"]
    assert await stand.member(5002) is None


async def test_the_user_file_needs_id_and_fio_columns(stand: Stand) -> None:
    report = await importer.load_users(stand.db, "Код;Имя\r\n1;А".encode(), "u.csv", NOW)
    assert report.problems == ["В первой строке нужны столбцы ID и ФИО"]
    report = await importer.load_users(stand.db, b"", "u.xlsx", NOW)
    assert report.problems == ["Нужен файл .csv со столбцами ID и ФИО"]


# ── Контакты ──────────────────────────────────────────────────────────────────


async def test_contacts_are_listed_by_name_and_load_back_as_users(stand: Stand) -> None:
    data = importer.contacts_csv(
        [
            UserCard(7002, "Петров", "Петр", "Петрович"),
            UserCard(7001, "Абрамов", "Антон", "Андреевич"),
        ]
    )
    assert data.decode("utf-8-sig") == (
        "ID;ФИО\r\n7001;Абрамов Антон Андреевич\r\n7002;Петров Петр Петрович\r\n"
    )
    report = await importer.load_users(stand.db, data, "контакты.csv", NOW)
    assert report.ok, report.problems
    assert report.rows == 2
