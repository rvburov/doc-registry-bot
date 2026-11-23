"""Подразделение из карточки, актуальность, права, история и совместимость файлов."""

import csv
import io
from importlib import import_module
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot import importer
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import Attachment, MessengerError
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import Member, Pending
from doc_registry_bot.registry import markup, schema
from tests import books
from tests.stand import ADMIN, OWNER, REGISTRAR, Stand

pytestmark = pytest.mark.asyncio
NUMBER = "АБВГ.301121.001"
DIVISION = "Отдел главного конструктора"
TARGET = "Петров Петр Петрович"
TARGET_DIVISION = "Отдел разработки"
SOURCES = ("сообщение", "список", "файл")


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    result.messenger.person(2002, "Петров", "Петр", "Петрович", TARGET_DIVISION)
    await result.add(NUMBER)
    return result


async def register(
    stand: Stand, sender: int, source: str, *, fio: str = "", division: str = ""
) -> str:
    if source == "файл":
        data = books.create(books.Row(2, fio=fio, division=division, status=schema.CANCELED))
        stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Заявка.xlsx", data)
        return await stand.say(sender, "", [7])
    number = "АБВГ.301121.ХХХ"
    if source == "список":
        number += ", АБВГ.301121.ХХХ"
    return await stand.say(
        sender,
        f"Действие: Регистрация\nНомер: {number}\nНаименование: Панель\nПроект: Стенд\n"
        f"ФИО: {fio}\n{schema.DIVISION}: {division}\n{schema.STATUS}: {schema.CANCELED}",
    )


@pytest.mark.parametrize("source", SOURCES)
async def test_user_registration_takes_division_from_api_and_ignores_supplied_status(
    stand: Stand, source: str
) -> None:
    answer = await register(stand, REGISTRAR, source, fio=TARGET, division="Подмена")
    assert "Зарегистрировано" in answer
    for entry in (await stand.entries())[1:]:
        assert entry.fio == stand.messenger.cards[REGISTRAR].fio
        assert entry.division == DIVISION
        assert entry.status == schema.REGISTERED
    for event in await stand.audit():
        assert event.actor_id == REGISTRAR
        assert f"подразделение: {DIVISION}" in event.details
        assert f"актуальность: {schema.REGISTERED}" in event.details


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_admin_registration_for_another_person_uses_their_api_division(
    stand: Stand, sender: int, source: str
) -> None:
    # В сообщении отсутствие строки, в файле пустая ячейка означают карточку сотрудника.
    if source == "файл":
        answer = await register(stand, sender, source, fio=TARGET)
    else:
        number = "АБВГ.301121.ХХХ" + (", АБВГ.301121.ХХХ" if source == "список" else "")
        answer = await stand.say(
            sender,
            f"Действие: Регистрация\nНомер: {number}\n"
            f"Наименование: Панель\nПроект: Стенд\nФИО: {TARGET}",
        )
    assert "Зарегистрировано" in answer
    for entry in (await stand.entries())[1:]:
        assert (entry.fio, entry.division, entry.status) == (
            TARGET,
            TARGET_DIVISION,
            schema.REGISTERED,
        )
        assert entry.registrant_id == sender
    assert all(e.actor_id == sender for e in await stand.audit())


@pytest.mark.parametrize("source", SOURCES)
async def test_admin_can_supply_division_explicitly(stand: Stand, source: str) -> None:
    assert "Зарегистрировано" in await register(stand, ADMIN, source, fio=TARGET, division="Архив")
    assert all(e.division == "Архив" for e in (await stand.entries())[1:])


@pytest.mark.parametrize("source", SOURCES)
async def test_blank_registration_division_uses_the_target_card(stand: Stand, source: str) -> None:
    assert "Зарегистрировано" in await register(stand, ADMIN, source, fio=TARGET, division="")
    assert all(e.division == TARGET_DIVISION for e in (await stand.entries())[1:])


async def test_duplicate_names_do_not_select_a_department_arbitrarily(stand: Stand) -> None:
    stand.messenger.person(2003, "Петров", "Петр", "Петрович", "Другой отдел")
    answer = await stand.say(
        ADMIN,
        f"Действие: Регистрация\nНомер: АБВГ.301121.ХХХ\n"
        f"Наименование: Панель\nПроект: Стенд\nФИО: {TARGET}",
    )
    assert "Зарегистрировано" in answer
    assert (await stand.entries())[-1].division == ""


async def test_a_contact_service_failure_does_not_register_a_partial_file(stand: Stand) -> None:
    stand.messenger.contacts = AsyncMock(side_effect=MessengerError("сбой"))
    answer = await register(stand, ADMIN, "файл", fio=TARGET)
    assert "не удалось получить подразделение" in markup.plain(answer)
    assert len(await stand.entries()) == 1
    assert await stand.audit() == []


async def test_a_file_fetches_contact_divisions_once_and_keeps_row_values(stand: Stand) -> None:
    original = stand.messenger.contacts
    contacts = AsyncMock(side_effect=original)
    stand.messenger.contacts = contacts
    data = books.create(
        books.Row(2, fio=TARGET),
        books.Row(3, fio=TARGET, division="Архив"),
        books.Row(4, fio=TARGET),
    )
    stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Заявка.xlsx", data)
    assert "Зарегистрировано" in await stand.say(ADMIN, "", [7])
    assert [entry.division for entry in (await stand.entries())[1:]] == [
        TARGET_DIVISION,
        "Архив",
        TARGET_DIVISION,
    ]
    assert contacts.await_count == 1


async def test_api_card_changes_are_used_by_the_next_registration(stand: Stand) -> None:
    assert "Зарегистрировано" in await register(stand, REGISTRAR, "сообщение")
    stand.messenger.person(REGISTRAR, "Иванов", "Иван", "Иванович", "Новый отдел")
    assert "Зарегистрировано" in await register(stand, REGISTRAR, "сообщение")
    assert [e.division for e in (await stand.entries())[1:]] == [DIVISION, "Новый отдел"]


@pytest.mark.parametrize("source", SOURCES)
async def test_division_override_alone_requires_admin_rights_at_confirmation(
    stand: Stand, source: str
) -> None:
    # Новая серия, чтобы регистрация ждала «Да», даже без изменения ФИО.
    data = books.create(books.Row(2, number="АБВГ.806121.ХХХ", fio="", division="Архив"))
    if source == "файл":
        stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Заявка.xlsx", data)
        preview = await stand.say(ADMIN, "", [7])
    else:
        number = "АБВГ.806121.ХХХ" + (", АБВГ.806121.ХХХ" if source == "список" else "")
        preview = await stand.say(
            ADMIN,
            f"Действие: Регистрация\nНомер: {number}\n"
            f"Наименование: Панель\nПроект: Стенд\n{schema.DIVISION}: Архив",
        )
    assert "Подтвердите:" in preview
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
    assert "Нет прав" in await stand.say(ADMIN, "Да")
    assert await stand.designations() == [NUMBER]
    assert await stand.audit() == []


def edit(field: str, value: str) -> str:
    return f"Действие: Изменение\nНомер: {NUMBER}\n{field}: {value}\nПроект: Новый проект"


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
@pytest.mark.parametrize(
    "field,value,attribute",
    [
        (schema.DIVISION, TARGET_DIVISION, "division"),
        (schema.STATUS, schema.CANCELED, "status"),
    ],
)
async def test_admin_changes_protected_fields_only_after_confirmation_and_audits_values(
    stand: Stand, sender: int, field: str, value: str, attribute: str
) -> None:
    [before] = await stand.entries()
    preview = markup.plain(await stand.say(sender, edit(field, value)))
    original = getattr(before, attribute) or "не указано"
    assert f"{field}: {original} → {value}" in preview
    assert getattr((await stand.entries())[0], attribute) == getattr(before, attribute)
    assert await stand.audit() == []
    assert "Изменено" in await stand.say(sender, "Да")
    [after] = await stand.entries()
    assert getattr(after, attribute) == value
    assert after.registered_at == before.registered_at
    assert after.registrant_id == before.registrant_id
    assert after.version == before.version + 1
    [event] = await stand.audit()
    assert event.actor_id == sender
    assert event.actor_fio == stand.messenger.cards[sender].fio
    assert f"{field}: {original} → {value}" in event.details


@pytest.mark.parametrize(
    "field,value", [(schema.DIVISION, "Архив"), (schema.STATUS, schema.CANCELED)]
)
async def test_user_cannot_change_protected_fields_or_accompanying_project(
    stand: Stand, field: str, value: str
) -> None:
    answer = markup.plain(await stand.say(REGISTRAR, edit(field, value)))
    assert "только администратору" in answer
    [entry] = await stand.entries()
    assert entry.status == schema.REGISTERED and entry.division == "" and entry.project == "Машина"
    assert await stand.audit() == []


@pytest.mark.parametrize(
    "field,value", [(schema.DIVISION, "Архив"), (schema.STATUS, schema.CANCELED)]
)
async def test_removed_admin_cannot_confirm_protected_changes(
    stand: Stand, field: str, value: str
) -> None:
    assert "Подтвердите изменение" in await stand.say(ADMIN, edit(field, value))
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
    assert "Нет прав" in await stand.say(ADMIN, "Да")
    [entry] = await stand.entries()
    assert entry.status == schema.REGISTERED and entry.division == "" and entry.project == "Машина"
    assert await stand.audit() == []


@pytest.mark.parametrize("value", ["", "Удален", "В работе"])
async def test_invalid_status_refuses_entire_edit(stand: Stand, value: str) -> None:
    assert "допустимо только" in await stand.say(ADMIN, edit(schema.STATUS, value))
    [entry] = await stand.entries()
    assert entry.status == schema.REGISTERED and entry.project == "Машина"
    assert await stand.audit() == []


async def test_admin_can_clear_division_but_user_edit_preserves_both_fields(stand: Stand) -> None:
    await stand.say(ADMIN, edit(schema.DIVISION, TARGET_DIVISION))
    await stand.say(ADMIN, "Да")
    await stand.say(ADMIN, edit(schema.STATUS, schema.CANCELED))
    await stand.say(ADMIN, "Да")
    await stand.say(REGISTRAR, f"Действие: Изменение\nНомер: {NUMBER}\nНаименование: Панель")
    await stand.say(REGISTRAR, "Да")
    [entry] = await stand.entries()
    assert (entry.division, entry.status) == (TARGET_DIVISION, schema.CANCELED)
    await stand.say(ADMIN, edit(schema.DIVISION, ""))
    assert "Изменено" in await stand.say(ADMIN, "Да")
    assert (await stand.entries())[0].division == ""


@pytest.mark.parametrize(
    "field,value", [(schema.DIVISION, "Архив"), (schema.STATUS, schema.CANCELED)]
)
async def test_cancelling_protected_edit_leaves_no_audit_or_changes(
    stand: Stand, field: str, value: str
) -> None:
    await stand.say(ADMIN, edit(field, value))
    assert "Операция отменена" in await stand.say(ADMIN, "Нет")
    [entry] = await stand.entries()
    assert entry.division == "" and entry.status == schema.REGISTERED
    assert await stand.audit() == []


async def test_annulled_number_stays_occupied_and_status_can_be_restored(stand: Stand) -> None:
    await stand.say(ADMIN, edit(schema.STATUS, schema.CANCELED))
    await stand.say(ADMIN, "Да")
    assert "Зарегистрировано" in await register(stand, REGISTRAR, "сообщение")
    assert await stand.designations() == [NUMBER, "АБВГ.301121.002"]
    view = await stand.say(REGISTRAR, f"Действие: Просмотр\nНомер: {NUMBER}")
    assert schema.CANCELED in view
    await stand.say(ADMIN, edit(schema.STATUS, schema.REGISTERED))
    assert "Изменено" in await stand.say(ADMIN, "Да")
    assert (await stand.entries())[0].status == schema.REGISTERED


async def test_export_import_keeps_ten_columns_values_and_fingerprint(stand: Stand) -> None:
    async with stand.db() as session, session.begin():
        entry = await journal.find(session, NUMBER)
        assert entry is not None
        entry.division = TARGET_DIVISION
        entry.status = schema.CANCELED
        data = await importer.export_csv(session, stand.dispatcher._config.zone)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == [
        "Наименование",
        "Обозначение",
        "Проект",
        "Дата регистрации",
        "ФИО регистрирующего",
        "Подразделение регистрирующего",
        "Актуальность",
        "Отметка учета",
        "Примечание первичное",
        "Примечание краткая характеристика",
    ]
    assert rows[1][5:7] == [TARGET_DIVISION, schema.CANCELED]
    entries, report = importer.read_journal(
        data, "Журнал.csv", stand.dispatcher._config.zone, stand.now
    )
    assert report.ok
    assert (entries[0].division, entries[0].status) == (TARGET_DIVISION, schema.CANCELED)
    old_fingerprint = importer.fingerprint(data)
    async with stand.db() as session, session.begin():
        entry = await journal.find(session, NUMBER)
        assert entry is not None
        entry.division = "Архив"
        assert (
            importer.fingerprint(await importer.export_csv(session, stand.dispatcher._config.zone))
            != old_fingerprint
        )


async def test_a_pending_note_edit_from_the_previous_version_can_be_confirmed(stand: Stand) -> None:
    assert "Подтвердите изменение" in await stand.say(REGISTRAR, edit(schema.SUMMARY, "Сталь"))
    async with stand.db() as session, session.begin():
        pending = await session.get(Pending, REGISTRAR)
        assert pending is not None
        payload = dict(pending.payload)
        card = dict(payload["card"])
        card.pop("division")
        card.pop("status")
        payload["card"] = card
        payload["changes"] = {schema.LEGACY_COLUMNS[-1]: "Сталь", "Проект": "Новый проект"}
        pending.payload = payload
    assert "Изменено" in await stand.say(REGISTRAR, "Да")
    [entry] = await stand.entries()
    assert entry.summary == "Сталь" and entry.status == schema.REGISTERED
    [event] = await stand.audit()
    assert f"{schema.SUMMARY}: не указано → Сталь" in event.details


@pytest.mark.parametrize("status", [schema.REGISTERED, schema.CANCELED, "", "Ошибка"])
async def test_import_validates_status_and_preserves_database_on_refusal(
    stand: Stand, status: str
) -> None:
    values = ["Панель", NUMBER, "Стенд", "19.11.2026", TARGET, TARGET_DIVISION, status, "", "", ""]
    data = importer.csv_bytes(list(schema.COLUMNS), [values])
    entries, report = importer.read_journal(
        data, "Журнал.csv", stand.dispatcher._config.zone, stand.now
    )
    assert report.ok == (status in schema.STATUSES)
    assert bool(entries) == report.ok
    assert (await stand.entries())[0].status == schema.REGISTERED


async def test_additive_migration_preserves_legacy_rows_and_can_run_again(migrated: str) -> None:
    engine = create_engine(migrated)
    migration = import_module("doc_registry_bot.migrations.versions.0003_journal_fields")
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA fields_migration_test"))
        connection.execute(text("SET LOCAL search_path TO fields_migration_test"))
        connection.execute(text("CREATE TABLE journal (id bigint PRIMARY KEY, fio text NOT NULL)"))
        connection.execute(text("INSERT INTO journal VALUES (9, 'Петров Петр Петрович')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            defaults = connection.execute(text("SELECT id,fio,division,status FROM journal")).one()
            assert tuple(defaults) == (9, TARGET, "", schema.REGISTERED)
            connection.execute(text("UPDATE journal SET division='Архив', status='Аннулирован'"))
            migration.upgrade()
            migration.downgrade()
        row = connection.execute(text("SELECT id,fio,division,status FROM journal")).one()
        assert tuple(row) == (9, TARGET, "Архив", schema.CANCELED)
        connection.execute(text("DROP SCHEMA fields_migration_test CASCADE"))
    engine.dispose()
