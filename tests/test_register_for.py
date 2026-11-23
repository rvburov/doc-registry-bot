"""Администратор регистрирует за другого человека, исполнитель остается в истории."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import Attachment
from doc_registry_bot.db.models import Member
from doc_registry_bot.registry import markup, schema
from tests import books
from tests.stand import ADMIN, OWNER, REGISTRAR, Stand, registration

pytestmark = pytest.mark.asyncio

TARGET = "Петров Петр Петрович"
SECOND = "Сидоров Сидор Сидорович"
SOURCES = ["сообщение", "список", "файл"]


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    return result


async def request(
    stand: Stand,
    sender: int,
    source: str,
    fio: str | None = TARGET,
    second_fio: str | None = None,
) -> str:
    if source == "файл":
        data = books.create(
            books.Row(2, name="Кронштейн", fio=fio or "", project="Машина"),
            books.Row(
                5,
                name="Вал",
                fio=(fio or "") if second_fio is None else second_fio,
                project="Машина",
            ),
        )
        stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Список.xlsx", data)
        return await stand.say(sender, "", [7])
    number = "АБВГ.301121.ХХХ"
    if source == "список":
        number += ", АБВГ.301121.ХХХ"
    body = registration(number)
    if fio is not None:
        body += f"\nФИО: {fio}"
    return await stand.say(sender, body)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("sender", [ADMIN, OWNER])
@pytest.mark.parametrize("new_series", [False, True])
async def test_admin_registers_for_other_people_and_remains_the_actual_actor(
    stand: Stand, source: str, sender: int, new_series: bool
) -> None:
    if not new_series:
        await stand.add("АБВГ.301121.001")
    answer = await request(stand, sender, source, second_fio=SECOND)
    if new_series:
        assert "Заявка на регистрацию" in answer
        assert TARGET in markup.plain(answer)
        if source == "файл":
            assert SECOND in markup.plain(answer)
        assert await stand.designations() == []
        assert await stand.audit() == []
        answer = await stand.say(sender, "Да")
    assert "Зарегистрировано" in answer
    assert TARGET in markup.plain(answer)
    if source == "файл":
        assert SECOND in markup.plain(answer)

    count = 1 if source == "сообщение" else 2
    start = 1 if new_series else 2
    designations = [f"АБВГ.301121.{n:03d}" for n in range(start, start + count)]
    expected_fio = [TARGET, SECOND] if source == "файл" else [TARGET] * count
    entries = (await stand.entries())[-count:]
    assert [entry.designation for entry in entries] == designations
    assert [entry.fio for entry in entries] == expected_fio
    assert all(entry.registrant_id == sender for entry in entries)
    actor_fio = stand.messenger.cards[sender].fio
    events = await stand.audit()
    assert len(events) == count
    for event, designation, fio in zip(events, designations, expected_fio, strict=True):
        assert event.action == "Регистрация записи"
        assert event.actor_id == sender
        assert event.actor_fio == actor_fio
        assert designation in event.details
        assert fio in event.details

    # Повторная доставка выполненного сообщения не регистрирует еще один документ.
    assert await stand.say(sender, "Да", message_id=stand.message_id) == ""
    assert len(await stand.audit()) == count
    assert len(await stand.entries()) == count + (0 if new_series else 1)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("sender", [ADMIN, OWNER])
@pytest.mark.parametrize("fio", [None, ""])
async def test_admin_uses_own_card_when_target_fio_is_missing_or_empty(
    stand: Stand, source: str, sender: int, fio: str | None
) -> None:
    await stand.add("АБВГ.301121.001")
    assert "Зарегистрировано" in await request(stand, sender, source, fio=fio)
    count = 1 if source == "сообщение" else 2
    own_fio = stand.messenger.cards[sender].fio
    assert [entry.fio for entry in (await stand.entries())[-count:]] == [own_fio] * count
    assert all(event.actor_fio == own_fio for event in await stand.audit())


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_admin_can_register_a_book_without_a_fio_column(stand: Stand, sender: int) -> None:
    await stand.add("АБВГ.301121.001")
    header = [column for column in schema.LEGACY_COLUMNS if column != schema.FIO]

    def fill(sheet: Any) -> None:
        values = ["Кронштейн", "АБВГ.301121.ХХХ", "", "Машина", "", "", ""]
        for column, value in enumerate(values, start=1):
            if value:
                sheet.cell(row=2, column=column, value=value)

    data = books.create_sheet(fill, header)
    stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Без ФИО.xlsx", data)
    assert "Зарегистрировано" in await stand.say(sender, "", [7])
    [entry] = (await stand.entries())[1:]
    assert entry.fio == stand.messenger.cards[sender].fio
    assert entry.registrant_id == sender


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("fio", [TARGET, "Петров П.П."])
async def test_regular_user_cannot_replace_their_card_name(
    stand: Stand, source: str, fio: str
) -> None:
    await stand.add("АБВГ.301121.001")
    assert "Зарегистрировано" in await request(stand, REGISTRAR, source, fio=fio, second_fio=SECOND)
    count = 1 if source == "сообщение" else 2
    own_fio = stand.messenger.cards[REGISTRAR].fio
    entries = (await stand.entries())[-count:]
    assert [entry.fio for entry in entries] == [own_fio] * count
    assert all(entry.registrant_id == REGISTRAR for entry in entries)
    for event in await stand.audit():
        assert event.actor_id == REGISTRAR
        assert event.actor_fio == own_fio
        assert TARGET not in event.details
        assert SECOND not in event.details


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_incomplete_target_name_rejects_the_entire_admin_request(
    stand: Stand, source: str, sender: int
) -> None:
    await stand.add("АБВГ.301121.001")
    # В книге первая строка допустима, ошибка второй должна отменить обе регистрации.
    first = TARGET if source == "файл" else "Петров П.П."
    answer = await request(stand, sender, source, fio=first, second_fio="Петров П.П.")
    assert "ФИО:" in markup.plain(answer)
    assert "не полное ФИО" in markup.plain(answer)
    assert await stand.designations() == ["АБВГ.301121.001"]
    assert await stand.audit() == []


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_an_admin_needs_their_own_full_card_name_even_when_registering_for_others(
    stand: Stand, sender: int
) -> None:
    stand.messenger.person(sender, "Админов", "Андрей", "")
    answer = await request(stand, sender, "сообщение")
    assert "нет полного ФИО" in markup.plain(answer)
    assert await stand.designations() == []
    assert await stand.audit() == []


@pytest.mark.parametrize("source", SOURCES)
async def test_admin_rights_are_checked_again_before_confirming_for_another_person(
    stand: Stand, source: str
) -> None:
    answer = await request(stand, ADMIN, source, second_fio=SECOND)
    assert "Заявка на регистрацию" in answer
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
        # Обычный доступ остается: проверяем потерю права регистрации за других.
        assert member.has_access
    answer = await stand.say(ADMIN, "Да")
    assert "Зарегистрировано" not in answer
    assert await stand.designations() == []
    assert await stand.audit() == []


@pytest.mark.parametrize("source", SOURCES)
async def test_promotion_does_not_restore_a_fio_ignored_in_the_original_user_request(
    stand: Stand, source: str
) -> None:
    answer = await request(stand, REGISTRAR, source, second_fio=SECOND)
    assert "Заявка на регистрацию" in answer
    async with stand.db() as session, session.begin():
        member = await session.get(Member, REGISTRAR)
        assert member is not None
        member.is_admin = True
    assert "Зарегистрировано" in await stand.say(REGISTRAR, "Да")
    own_fio = stand.messenger.cards[REGISTRAR].fio
    assert all(entry.fio == own_fio for entry in await stand.entries())
    assert all(event.actor_fio == own_fio for event in await stand.audit())


@pytest.mark.parametrize("source", SOURCES)
async def test_canceling_registration_for_another_person_leaves_no_entry_or_event(
    stand: Stand, source: str
) -> None:
    assert "Заявка на регистрацию" in await request(stand, ADMIN, source)
    assert "Операция отменена" in await stand.say(ADMIN, "Нет")
    assert await stand.designations() == []
    assert await stand.audit() == []
