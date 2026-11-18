"""Необязательные поля журнала: регистрация, просмотр, выгрузка и правка через сообщения."""

import csv
import io
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import Attachment
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import JournalEntry, Member, Pending
from doc_registry_bot.registry import markup, schema
from tests import books
from tests.stand import ADMIN, REGISTRAR, Stand, registration

pytestmark = pytest.mark.asyncio

NUMBER = "АБВГ.301121.001"
TARGET = "Петров Петр Петрович"
FIELDS = (
    ("accounting", schema.ACCOUNTING, "Учтен", "Не учтен"),
    ("first_use", schema.FIRST_USE, "Изделие", "Стенд"),
    ("summary", schema.SUMMARY, "Сталь", "Алюминий"),
)
VALUES = {field: value for _, field, value, _ in FIELDS}
ALL_FIELDS = (
    "id",
    "designation",
    "designation_key",
    "series",
    "name",
    "fio",
    "registered_at",
    "project",
    "accounting",
    "first_use",
    "summary",
    "registrant_id",
    "version",
    "updated_at",
)


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    return result


def snapshot(entry: JournalEntry) -> tuple[object, ...]:
    return tuple(getattr(entry, field) for field in ALL_FIELDS)


def with_fields(body: str, values: dict[str, str]) -> str:
    return body + "".join(f"\n{field}: {value}" for field, value in values.items())


def request(source: str, values: dict[str, str]) -> str:
    numbers = "АБВГ.301121.ХХХ"
    if source == "список":
        numbers += ", АБВГ.301121.ХХХ"
    return with_fields(registration(numbers), values)


def edit(values: dict[str, str]) -> str:
    return with_fields(f"Действие: Изменение\nНомер: {NUMBER}", values)


async def seed(stand: Stand) -> JournalEntry:
    await stand.add(NUMBER)
    async with stand.db() as session, session.begin():
        entry = await journal.find(session, NUMBER)
        assert entry is not None
        for attr, _, value, _ in FIELDS:
            setattr(entry, attr, value)
        entry.registered_at = stand.now - timedelta(days=10)
        entry.updated_at = entry.registered_at
        entry.registrant_id = REGISTRAR
        entry.version = 4
    return (await stand.entries())[0]


@pytest.mark.parametrize("source", ["сообщение", "список"])
@pytest.mark.parametrize("new_series", [False, True])
async def test_registration_fields_survive_confirmation_and_appear_in_view_and_csv(
    stand: Stand, source: str, new_series: bool
) -> None:
    if not new_series:
        await stand.add(NUMBER)
    answer = await stand.say(REGISTRAR, request(source, VALUES))
    if new_series:
        assert "Заявка на регистрацию" in answer
        assert await stand.entries() == []
        answer = await stand.say(REGISTRAR, "Да")
    assert "Зарегистрировано" in answer
    count = 1 if source == "сообщение" else 2
    entries = (await stand.entries())[-count:]
    for entry in entries:
        assert all(getattr(entry, attr) == value for attr, _, value, _ in FIELDS)
        assert entry.fio == stand.messenger.cards[REGISTRAR].fio
    viewed = markup.plain(
        await stand.say(REGISTRAR, f"Действие: Просмотр\nНомер: {entries[-1].designation}")
    )
    for _, field, value, _ in FIELDS:
        assert f"{field}: {value}" in viewed

    await stand.say(ADMIN, "Выгрузить журнал")
    [reply] = stand.replies
    [file] = reply.files
    rows = list(csv.DictReader(io.StringIO(file.data.decode("utf-8-sig")), delimiter=";"))
    wanted = {entry.designation for entry in entries}
    actual = [row for row in rows if row[schema.DESIGNATION] in wanted]
    assert len(actual) == count
    for row in actual:
        assert all(row[field] == value for _, field, value, _ in FIELDS)


@pytest.mark.parametrize("source", ["сообщение", "список"])
@pytest.mark.parametrize("empty", [False, True])
async def test_missing_or_empty_registration_fields_are_saved_as_empty_strings(
    stand: Stand, source: str, empty: bool
) -> None:
    await stand.add(NUMBER)
    values = {field: "" for _, field, _, _ in FIELDS} if empty else {}
    assert "Зарегистрировано" in await stand.say(REGISTRAR, request(source, values))
    count = 1 if source == "сообщение" else 2
    for entry in (await stand.entries())[-count:]:
        assert all(getattr(entry, attr) == "" for attr, _, _, _ in FIELDS)


@pytest.mark.parametrize("source", ["сообщение", "список"])
async def test_registration_fields_do_not_replace_the_admins_target_fio(
    stand: Stand, source: str
) -> None:
    await stand.add(NUMBER)
    values = {**VALUES, "ФИО": TARGET}
    assert "Зарегистрировано" in await stand.say(ADMIN, request(source, values))
    count = 1 if source == "сообщение" else 2
    for entry in (await stand.entries())[-count:]:
        assert entry.fio == TARGET
        assert entry.registrant_id == ADMIN
        assert all(getattr(entry, attr) == value for attr, _, value, _ in FIELDS)
    assert all(event.actor_id == ADMIN for event in await stand.audit())


@pytest.mark.parametrize("source", ["сообщение", "список"])
async def test_old_registration_confirmations_without_new_fields_remain_valid(
    stand: Stand, source: str
) -> None:
    assert "Заявка на регистрацию" in await stand.say(REGISTRAR, request(source, {}))
    # Старые заявки в JSON не содержали этих значений ни в строках, ни в одиночной заявке.
    attrs = {attr for attr, _, _, _ in FIELDS}
    async with stand.db() as session, session.begin():
        pending = await session.get(Pending, REGISTRAR)
        assert pending is not None
        payload = dict(pending.payload)
        payload["rows"] = [
            {key: value for key, value in row.items() if key not in attrs}
            for row in payload["rows"]
        ]
        if "request" in payload:
            payload["request"] = {
                key: value for key, value in payload["request"].items() if key not in attrs
            }
        pending.payload = payload
    assert "Зарегистрировано" in await stand.say(REGISTRAR, "Да")
    assert all(
        getattr(entry, attr) == "" for entry in await stand.entries() for attr, _, _, _ in FIELDS
    )


@pytest.mark.parametrize("sender", [REGISTRAR, ADMIN])
async def test_book_registration_keeps_its_own_optional_values(stand: Stand, sender: int) -> None:
    await stand.add(NUMBER)
    data = books.create(
        books.Row(2, fio=TARGET, accounting="Учтен", first_use="Изделие", summary="Сталь"),
        books.Row(5, fio=TARGET, accounting="Не учтен", first_use="Стенд", summary="Алюминий"),
    )
    stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Список.xlsx", data)
    assert "Зарегистрировано" in await stand.say(sender, "", [7])
    first, second = (await stand.entries())[1:]
    assert (first.accounting, first.first_use, first.summary) == ("Учтен", "Изделие", "Сталь")
    assert (second.accounting, second.first_use, second.summary) == (
        "Не учтен",
        "Стенд",
        "Алюминий",
    )


@pytest.mark.parametrize("attr,field,old,new", FIELDS)
@pytest.mark.parametrize("clear", [False, True])
async def test_user_can_set_or_clear_each_optional_field_after_confirmation(
    stand: Stand, attr: str, field: str, old: str, new: str, *, clear: bool
) -> None:
    before = await seed(stand)
    target = "" if clear else new
    preview = markup.plain(await stand.say(REGISTRAR, edit({field: target})))
    assert f"{field}: {old} → {target or 'не указано'}" in preview
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []
    done = markup.plain(await stand.say(REGISTRAR, "Да"))
    assert done.startswith("Изменено\n")
    assert f"{field}: {target or 'не указано'}" in done
    [after] = await stand.entries()
    assert getattr(after, attr) == target
    for preserved in ALL_FIELDS:
        if preserved not in {attr, "version", "updated_at"}:
            assert getattr(after, preserved) == getattr(before, preserved)
    assert after.version == before.version + 1
    assert after.updated_at == stand.now
    [event] = await stand.audit()
    assert event.action == "Изменение записи"
    assert event.actor_id == REGISTRAR
    assert event.actor_fio == stand.messenger.cards[REGISTRAR].fio
    assert f"{field}: {old} →" in event.details
    if target:
        assert target in event.details
    assert await stand.say(REGISTRAR, "Да", message_id=stand.message_id) == ""
    assert snapshot((await stand.entries())[0]) == snapshot(after)
    assert len(await stand.audit()) == 1


async def test_whitespace_only_optional_value_clears_the_field(stand: Stand) -> None:
    await seed(stand)
    preview = markup.plain(await stand.say(REGISTRAR, edit({schema.SUMMARY: "   "})))
    assert f"{schema.SUMMARY}: Сталь → не указано" in preview
    assert "Изменено" in await stand.say(REGISTRAR, "Да")
    assert (await stand.entries())[0].summary == ""


async def test_omitted_optional_values_survive_a_project_edit(stand: Stand) -> None:
    before = await seed(stand)
    assert "Подтвердите изменение" in await stand.say(REGISTRAR, edit({"Проект": "Станок"}))
    assert "Изменено" in await stand.say(REGISTRAR, "Да")
    [after] = await stand.entries()
    assert after.project == "Станок"
    assert all(getattr(after, attr) == getattr(before, attr) for attr, _, _, _ in FIELDS)
    assert after.fio == before.fio


@pytest.mark.parametrize("field", ["Наименование", "Проект"])
async def test_empty_required_value_refuses_an_optional_edit_too(stand: Stand, field: str) -> None:
    before = await seed(stand)
    answer = markup.plain(
        await stand.say(REGISTRAR, edit({field: "", schema.SUMMARY: "Новое примечание"}))
    )
    assert f"{field}: не указано значение" in answer
    assert "Подтвердите изменение" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


@pytest.mark.parametrize("outcome", ["нет", "просрочено", "изменилось"])
async def test_unconfirmed_or_stale_optional_edits_do_not_change_data(
    stand: Stand, outcome: str
) -> None:
    await seed(stand)
    assert "Подтвердите изменение" in await stand.say(
        REGISTRAR, edit({schema.ACCOUNTING: "Не учтен", schema.SUMMARY: "Алюминий"})
    )
    if outcome == "просрочено":
        stand.later(11)
    elif outcome == "изменилось":
        async with stand.db() as session, session.begin():
            entry = await journal.find(session, NUMBER)
            assert entry is not None
            entry.project = "Другой проект"
            entry.version += 1
    [expected] = await stand.entries()
    answer = await stand.say(REGISTRAR, "Нет" if outcome == "нет" else "Да")
    assert "Изменено" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(expected)
    assert await stand.audit() == []


async def test_revoked_admin_cannot_confirm_fio_with_an_optional_field(stand: Stand) -> None:
    before = await seed(stand)
    assert "Подтвердите изменение" in await stand.say(
        ADMIN, edit({"ФИО": TARGET, schema.SUMMARY: "Алюминий"})
    )
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
    assert "Изменено" not in await stand.say(ADMIN, "Да")
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


async def test_revoked_admin_keeps_the_right_to_confirm_only_an_optional_field(
    stand: Stand,
) -> None:
    await seed(stand)
    assert "Подтвердите изменение" in await stand.say(ADMIN, edit({schema.SUMMARY: "Алюминий"}))
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
        assert member.has_access
    assert "Изменено" in await stand.say(ADMIN, "Да")
    assert (await stand.entries())[0].summary == "Алюминий"


async def test_regular_user_cannot_mix_a_fio_edit_with_an_optional_field(stand: Stand) -> None:
    before = await seed(stand)
    answer = markup.plain(
        await stand.say(REGISTRAR, edit({"ФИО": TARGET, schema.SUMMARY: "Алюминий"}))
    )
    assert "ФИО: регистрирующий определяется по карточке отправителя" in answer
    assert "Подтвердите изменение" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []
