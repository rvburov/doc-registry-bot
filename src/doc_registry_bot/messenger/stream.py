"""Поток событий службы мессенджера: STOMP поверх WebSocket, путь /notify, тема /topic/messages.

Обрыв связи — переподключение с паузой 5, 10, 20... до 60 секунд, как в SmartBase. О каждом
подключении и обрыве узнает служба бота: после подключения она дочитывает пропущенное.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import websockets

from doc_registry_bot.messenger import stomp
from doc_registry_bot.messenger.client import event_items

log = logging.getLogger(__name__)

TOPICS = ["/topic/messages"]

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
StateHandler = Callable[[bool], Awaitable[None]]


def socket_url(base_url: str) -> tuple[str, str]:
    """Адрес потока и имя хоста для кадра CONNECT."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}/notify", parts.hostname or "localhost"


class EventStream:
    def __init__(self, base_url: str, on_event: EventHandler, on_state: StateHandler) -> None:
        self._url, self._host = socket_url(base_url)
        self._on_event = on_event
        self._on_state = on_state
        self.first_pause = 5.0
        self.max_pause = 60.0

    async def run(self, stop: asyncio.Event) -> None:
        pause = self.first_pause
        while not stop.is_set():
            connected = False
            try:
                async with websockets.connect(self._url, open_timeout=15) as socket:
                    for item in stomp.greeting(self._host, TOPICS):
                        await socket.send(item)
                    connected = True
                    pause = self.first_pause
                    await self._on_state(True)
                    await self._listen(socket, stop)
            except (OSError, TimeoutError, websockets.WebSocketException) as error:
                log.warning("Поток событий: нет связи (%s)", error)
            if connected:
                await self._on_state(False)
            if stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=pause)
            pause = min(pause * 2, self.max_pause)

    async def _listen(self, socket: Any, stop: asyncio.Event) -> None:
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                receiving = asyncio.ensure_future(socket.recv())
                done, _ = await asyncio.wait(
                    {receiving, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if receiving not in done:
                    receiving.cancel()
                    return
                raw = receiving.result()
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                for frame in stomp.parse(text):
                    if frame.command.upper() == "ERROR":
                        log.warning("Поток событий: ошибка STOMP %s", frame.headers.get("message"))
                        continue
                    if frame.command.upper() != "MESSAGE" or not frame.body.strip():
                        continue
                    kind, rows = event_items(frame.body)
                    for row in rows:
                        await self._on_event(kind, row)
        finally:
            stopping.cancel()
