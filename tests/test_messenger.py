"""Связь со службой мессенджера: запросы, файлы блоками, поток STOMP."""

import asyncio
import json
from typing import Any

import pytest

from doc_registry_bot.bot.types import Attachment, MessengerError
from doc_registry_bot.messenger import client, stomp
from doc_registry_bot.messenger.client import HttpMessenger
from doc_registry_bot.messenger.stream import EventStream, socket_url
from tests.fake_messenger.server import FakeService, FakeStream

pytestmark = pytest.mark.asyncio


def messenger(service: FakeService) -> HttpMessenger:
    result = HttpMessenger("http://messenger:8888", transport=service.transport())
    result.download_pause = 0.01
    return result


async def test_a_reply_is_put_to_the_person_with_br_line_breaks() -> None:
    service = FakeService()
    await messenger(service).send(1001, "<b>Зарегистрировано</b>\nНомер: <b>АБВГ.301121.001</b>")
    assert service.messages == [
        {
            "targetId": 1001,
            "targetType": "person",
            "message": "<b>Зарегистрировано</b><br>Номер: <b>АБВГ.301121.001</b>",
        }
    ]


async def test_files_are_declared_then_uploaded_in_one_megabyte_blocks() -> None:
    service = FakeService()
    data = bytes(range(256)) * 9000  # больше двух блоков
    await messenger(service).send(5, "Журнал", [Attachment("Журнал КД.csv", data)])
    [declared] = service.messages[0]["files"]
    assert declared["name"] == "Журнал КД.csv"
    assert declared["length"] == len(data)
    assert declared["directory"] is False
    offsets = [offset for _, _, offset, _ in service.uploads]
    assert offsets == [0, client.BLOCK_BYTES, 2 * client.BLOCK_BYTES]
    assert {message for message, _, _, _ in service.uploads} == {"msg-1"}
    assert b"".join(chunk for _, _, _, chunk in service.uploads) == data


async def test_an_empty_file_is_refused_before_sending() -> None:
    service = FakeService()
    with pytest.raises(MessengerError):
        await messenger(service).send(5, "x", [Attachment("Пусто.txt", b"")])
    assert service.messages == []


async def test_a_file_is_waited_for_and_named_from_unicode_escapes() -> None:
    service = FakeService()
    service.files[(10, 20)] = ("Список КД.xlsx", b"PK")
    service.not_ready[(10, 20)] = 2
    attachment = await messenger(service).download(10, 20)
    assert attachment.name == "Список КД.xlsx"
    assert attachment.data == b"PK"
    downloads = [r for r in service.requests if r.url.path == "/message/download"]
    assert len(downloads) == 3
    assert json.loads(downloads[0].content) == {"serverMsgId": 10, "serverFileId": 20}


async def test_a_file_that_never_appears_fails_after_the_wait() -> None:
    service = FakeService()
    reader = messenger(service)
    reader.download_wait = 0.03
    with pytest.raises(MessengerError):
        await reader.download(10, 99)


async def test_cards_and_contacts_are_read() -> None:
    service = FakeService()
    service.cards[1001] = {
        "userId": 1001,
        "family": "Иванов",
        "fname": "Иван",
        "sname": "Иванович",
        "division": "ОГК",
        "post": "инженер",
    }
    reader = messenger(service)
    card = await reader.user_card(1001)
    assert card is not None and card.fio == "Иванов Иван Иванович"
    assert card.division == "ОГК"
    assert await reader.user_card(42) is None
    [contact] = await reader.contacts()
    assert contact.division == "ОГК"


async def test_history_is_asked_after_a_server_time() -> None:
    service = FakeService()
    service.history[1001] = [
        {"serverMessageId": 1, "ownerId": 1001, "text": "старое", "ctime": 100},
        {
            "serverMessageId": 2,
            "ownerId": 1001,
            "text": "новое",
            "ctime": 300,
            "hasFile": True,
            "serverFileIds": [7],
        },
    ]
    [item] = await messenger(service).history(1001, 200, 100)
    assert item.message_id == 2 and item.file_ids == [7] and item.ctime == 300
    request = next(r for r in service.requests if r.url.path == "/message/history")
    assert json.loads(request.content) == {
        "targetId": 1001,
        "targetType": "person",
        "cTime": 200,
        "messageCount": 100,
        "getPreview": False,
    }


@pytest.mark.parametrize(
    ("header", "name"),
    [
        (None, ""),
        ('attachment; filename="Список.xlsx"', "Список.xlsx"),
        ("attachment; filename*=UTF-8''%D0%A1%D0%BF%D0%B8%D1%81%D0%BE%D0%BA.xlsx", "Список.xlsx"),
        ('"\\u0421\\u043f\\u0438\\u0441\\u043e\\u043a.xlsx"', "Список.xlsx"),
        ("attachment", ""),
        ('attachment; filename="../секрет"', ""),
    ],
)
async def test_file_names_are_read_safely(header: str | None, name: str) -> None:
    assert client.file_name(header) == name


@pytest.mark.parametrize(
    ("value", "ids"),
    [([1, 2], [1, 2]), (5, [5]), ("[3, 4]", [3, 4]), (None, []), ("", [])],
)
async def test_file_ids_come_in_any_shape(value: Any, ids: list[int]) -> None:
    assert client.file_ids_of(value) == ids


async def test_stomp_frames_are_connect_and_subscribe() -> None:
    connect, subscribe = stomp.greeting("localhost", ["/topic/messages"])
    assert connect == "CONNECT\naccept-version:1.2,1.1,1.0\nhost:localhost\nheart-beat:0,0\n\n\x00"
    assert subscribe == "SUBSCRIBE\nid:sub-0\ndestination:/topic/messages\nack:auto\n\n\x00"
    [frame] = stomp.parse('\nMESSAGE\r\ndestination:/topic/messages\r\n\r\n{"type":"X"}\x00')
    assert frame.command == "MESSAGE"
    assert frame.headers["destination"] == "/topic/messages"
    assert frame.body == '{"type":"X"}'


async def test_the_stream_address_follows_the_service_address() -> None:
    assert socket_url("http://messenger:8888") == ("ws://messenger:8888/notify", "messenger")
    assert socket_url("https://chat.local") == ("wss://chat.local/notify", "chat.local")


async def test_the_stream_subscribes_delivers_events_and_reconnects() -> None:
    server = FakeStream()
    await server.start()
    events: list[tuple[str, dict[str, Any]]] = []
    states: list[bool] = []

    async def on_event(kind: str, row: dict[str, Any]) -> None:
        events.append((kind, row))

    async def on_state(connected: bool) -> None:
        states.append(connected)

    stream = EventStream(f"http://127.0.0.1:{server.port}", on_event, on_state)
    stream.first_pause = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        await asyncio.wait_for(server.subscribed.wait(), 5)
        assert server.received[0].startswith("CONNECT\naccept-version:1.2,1.1,1.0\n")
        assert "destination:/topic/messages" in server.received[1]
        await server.publish(
            {"type": "INCOMING_MESSAGE", "data": [{"serverMessageId": 9, "ownerId": 1001}]}
        )
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.02)
        assert events == [("INCOMING_MESSAGE", {"serverMessageId": 9, "ownerId": 1001})]
        await server.drop()
        await asyncio.wait_for(server.subscribed.wait(), 5)
        assert states[:3] == [True, False, True]
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
        await server.stop()
