"""Служба: дочитывание за время простоя, уведомления владельцу, отчет о копии, чистка."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import HistoryMessage
from doc_registry_bot.db.models import AuditEntry
from doc_registry_bot.registry import markup
from doc_registry_bot.service import LAST_CTIME, BotService, incoming_of, server_time
from tests.stand import OWNER, REGISTRAR, Stand, registration

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def seconds(moment: datetime) -> int:
    return int(moment.timestamp())


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.add("АБВГ.301121.001")
    return result


def service_of(stand: Stand, config: BotConfig, **kwargs: object) -> BotService:
    service = BotService(stand.db, config, stand.messenger, **kwargs)  # type: ignore[arg-type]
    service.clock = lambda: NOW
    return service


async def drain(service: BotService) -> None:
    while not service._queue.empty():
        await service.process(service._queue.get_nowait())


async def test_server_time_reads_seconds_and_milliseconds() -> None:
    assert server_time(1_762_765_200) == datetime(2025, 11, 10, 9, 0, tzinfo=UTC)
    assert server_time(1_762_765_200_000) == datetime(2025, 11, 10, 9, 0, tzinfo=UTC)


async def test_only_incoming_messages_become_requests() -> None:
    row = {
        "serverMessageId": 7,
        "ownerId": 1001,
        "text": "Инструкция",
        "hasFile": True,
        "serverFileIds": "[5]",
        "ctime": 100,
    }
    incoming = incoming_of("INCOMING_MESSAGE", row)
    assert incoming is not None
    assert (incoming.message_id, incoming.sender_id, incoming.file_ids) == (7, 1001, [5])
    assert incoming_of("NEW_MESSAGE", row) is None
    assert incoming_of("INCOMING_MESSAGE", {"ownerId": 1}) is None


async def test_the_first_start_notifies_and_reads_no_history(
    stand: Stand, config: BotConfig
) -> None:
    stand.messenger.histories[REGISTRAR] = [
        HistoryMessage(1, REGISTRAR, registration(), seconds(NOW - timedelta(minutes=5)))
    ]
    service = service_of(stand, config)
    await service.on_state(True)
    await drain(service)
    assert [s.user_id for s in stand.messenger.sent] == [OWNER]
    assert "запущен, версия" in markup.plain(stand.messenger.sent[0].html)
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_missed_messages_are_caught_up_and_old_ones_are_asked_again(
    stand: Stand, config: BotConfig
) -> None:
    service = service_of(stand, config)
    await service._set_state(LAST_CTIME, str(seconds(NOW - timedelta(days=3))))
    stand.messenger.histories[REGISTRAR] = [
        HistoryMessage(1, REGISTRAR, registration(), seconds(NOW - timedelta(days=2))),
        HistoryMessage(2, 777, "ответ бота", seconds(NOW - timedelta(hours=2))),
        HistoryMessage(3, REGISTRAR, registration(), seconds(NOW - timedelta(hours=1))),
    ]
    await service.catch_up()
    await drain(service)
    assert await stand.designations() == ["АБВГ.301121.001", "АБВГ.301121.002"]
    texts = [markup.plain(s.html) for s in stand.messenger.sent if s.user_id == REGISTRAR]
    assert "Сообщение пришло, пока бот не работал. Отправьте его заново." in texts
    assert any(t.startswith("Зарегистрировано") for t in texts)
    assert int(await service._state(LAST_CTIME) or 0) == seconds(NOW - timedelta(hours=1))
    # Повторное дочитывание ничего не повторяет.
    stand.messenger.sent.clear()
    await service._set_state(LAST_CTIME, str(seconds(NOW - timedelta(days=3))))
    await service.catch_up()
    await drain(service)
    assert stand.messenger.sent == []
    assert len(await stand.designations()) == 2


async def test_a_long_outage_is_reported_to_the_owner(stand: Stand, config: BotConfig) -> None:
    service = service_of(stand, config)
    await service.on_state(True)
    await service.on_state(False)
    service.clock = lambda: NOW + timedelta(minutes=12)
    await service.on_state(True)
    notes = [markup.plain(s.html) for s in stand.messenger.sent if s.user_id == OWNER]
    assert notes[-1] == "Связь со службой мессенджера восстановлена. Перерыв: 12 мин."


async def test_a_short_outage_is_not_reported(stand: Stand, config: BotConfig) -> None:
    service = service_of(stand, config)
    await service.on_state(True)
    await service.on_state(False)
    service.clock = lambda: NOW + timedelta(minutes=2)
    await service.on_state(True)
    assert len(stand.messenger.sent) == 1


async def test_a_backup_that_missed_the_share_is_reported_once(
    stand: Stand, config: BotConfig, tmp_path: Path
) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "time": "2026-11-10T03:00:00",
                "ok": True,
                "share_ok": False,
                "message": "сетевая папка недоступна",
            }
        ),
        encoding="utf-8",
    )
    service = service_of(stand, config, backup_status=status)
    await service.check_backup()
    await service.check_backup()
    [note] = stand.messenger.sent
    assert markup.plain(note.html) == (
        "Резервная копия сделана, но не попала в сетевую папку: сетевая папка недоступна"
    )


async def test_cleaning_keeps_twelve_months_of_actions(stand: Stand, config: BotConfig) -> None:
    async with stand.db() as session, session.begin():
        session.add(AuditEntry(at=NOW - timedelta(days=400), actor_id=1, action="старое"))
        session.add(AuditEntry(at=NOW - timedelta(days=10), actor_id=1, action="свежее"))
    await service_of(stand, config).clean()
    assert [a.action for a in await stand.audit()] == ["свежее"]
