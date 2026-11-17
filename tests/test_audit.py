"""Журнал действий: регистрации и выгрузка с событием о самой выгрузке."""

import csv
import io

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig, Ctx
from doc_registry_bot.bot.types import Attachment
from doc_registry_bot.registry import markup
from tests import books
from tests.stand import ADMIN, REGISTRAR, Stand, registration

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    return result


async def request(stand: Stand, source: str, *, invalid: bool = False) -> str:
    number = "неверный номер" if invalid else "АБВГ.301121.ХХХ"
    if source == "файл":
        data = books.create(
            books.Row(2, name="Кронштейн", project="Машина"),
            books.Row(5, number=number, name="Кронштейн", project="Машина"),
        )
        stand.messenger.files[(stand.message_id + 1, 7)] = Attachment("Список.xlsx", data)
        return await stand.say(REGISTRAR, "", [7])
    if source == "список":
        number = f"АБВГ.301121.ХХХ, {number}"
    return await stand.say(REGISTRAR, registration(number))


@pytest.mark.parametrize("source", ["сообщение", "список", "файл"])
@pytest.mark.parametrize("new_series", [False, True])
async def test_each_registered_document_has_an_audit_entry(
    stand: Stand, source: str, new_series: bool
) -> None:
    if not new_series:
        await stand.add("АБВГ.301121.001")
    answer = await request(stand, source)
    if new_series:
        assert "Заявка на регистрацию" in answer
        assert await stand.audit() == []
        answer = await stand.say(REGISTRAR, "Да")
    assert "Зарегистрировано" in answer
    count = 1 if source == "сообщение" else 2
    start = 1 if new_series else 2
    expected = [f"АБВГ.301121.{number:03d}" for number in range(start, start + count)]
    events = await stand.audit()
    assert len(events) == count
    for event, designation in zip(events, expected, strict=True):
        assert event.action == "Регистрация записи"
        assert event.actor_id == REGISTRAR
        assert event.actor_fio == "Иванов Иван Иванович"
        assert event.at == stand.now
        assert designation in event.details
        assert "наименование: Кронштейн" in event.details
        assert "проект: Машина" in event.details
        assert f"способ: {source}" in event.details
    if source == "файл":
        assert all("файл: Список.xlsx" in event.details for event in events)
        assert "строка: 2" in events[0].details
        assert "строка: 5" in events[1].details
    elif source == "список":
        assert "позиция: 1" in events[0].details
        assert "позиция: 2" in events[1].details
    # Повторная доставка того же сообщения не создает новые события и документы.
    await stand.say(REGISTRAR, "Да", message_id=stand.message_id)
    assert len(await stand.audit()) == count
    assert await stand.designations() == ([] if new_series else ["АБВГ.301121.001"]) + expected


@pytest.mark.parametrize("source", ["сообщение", "список", "файл"])
async def test_canceled_registration_has_no_audit_entry(stand: Stand, source: str) -> None:
    await request(stand, source)
    assert await stand.audit() == []
    assert "Операция отменена" in await stand.say(REGISTRAR, "Нет")
    assert await stand.audit() == []
    assert await stand.designations() == []


@pytest.mark.parametrize("source", ["сообщение", "список", "файл"])
async def test_invalid_registration_has_no_audit_entry(stand: Stand, source: str) -> None:
    answer = await request(stand, source, invalid=True)
    assert "Номер:" in markup.plain(answer)
    assert await stand.audit() == []
    assert await stand.designations() == []


async def test_audit_failure_rolls_back_the_registration(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    await stand.add("АБВГ.301121.001")

    def broken_audit(self: Ctx, action: str, details: str = "") -> None:
        raise RuntimeError("Запись события недоступна")

    monkeypatch.setattr(Ctx, "audit", broken_audit)
    answer = await request(stand, "список")
    assert "Не удалось обработать сообщение" in answer
    assert await stand.designations() == ["АБВГ.301121.001"]
    assert await stand.audit() == []


async def test_an_audit_export_includes_its_own_event_and_does_not_repeat_it(stand: Stand) -> None:
    await stand.say(ADMIN, "Выгрузить журнал")
    stand.later(1)
    answer = await stand.say(ADMIN, "Журнал действий")
    assert "Журнал действий: 2 записей за год" in markup.plain(answer)
    [reply] = stand.replies
    [file] = reply.files
    rows = list(csv.reader(io.StringIO(file.data.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == ["Когда", "Кто", "ID", "Действие", "Подробности"]
    assert [row[3] for row in rows[1:]] == ["Выгрузка журнала", "Выгрузка журнала действий"]
    assert rows[2][:3] == ["10.11.2026 12:01", "Админов Андрей Андреевич", str(ADMIN)]
    assert "период:" in rows[2][4]
    assert "10.11.2026 12:01" in rows[2][4]
    assert "записей: 2" in rows[2][4]
    events = await stand.audit()
    assert len(events) == 2
    assert events[-1].details == rows[2][4]
    await stand.say(ADMIN, "Журнал действий", message_id=stand.message_id)
    assert len(await stand.audit()) == 2
    stand.later(1)
    await stand.say(ADMIN, "Журнал действий")
    assert len(await stand.audit()) == 3


async def test_a_forbidden_audit_export_is_not_logged(stand: Stand) -> None:
    assert "Нет прав" in await stand.say(REGISTRAR, "Журнал действий")
    assert await stand.audit() == []
