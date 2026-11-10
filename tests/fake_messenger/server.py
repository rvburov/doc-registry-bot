"""Подделка службы мессенджера для тестов: HTTP API через httpx.MockTransport и поток событий
STOMP через настоящий WebSocket-сервер на свободном порту."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from doc_registry_bot.messenger import stomp


@dataclass
class FakeService:
    """Что служба получила и что отдает."""

    cards: dict[int, dict[str, Any]] = field(default_factory=dict)
    files: dict[tuple[int, int], tuple[str, bytes]] = field(default_factory=dict)
    not_ready: dict[tuple[int, int], int] = field(default_factory=dict)
    history: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[tuple[str, str, int, bytes]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "PUT" and path == "/message":
            body = json.loads(request.content)
            self.messages.append(body)
            return httpx.Response(200, json={"uuid": f"msg-{len(self.messages)}"})
        if request.method == "POST" and path == "/message/file":
            self.uploads.append(
                (
                    request.url.params["msgUuid"],
                    request.url.params["fileUuid"],
                    int(request.headers["Upload-Offset"]),
                    request.content,
                )
            )
            return httpx.Response(200)
        if request.method == "POST" and path == "/message/download":
            body = json.loads(request.content)
            key = (body["serverMsgId"], body["serverFileId"])
            if self.not_ready.get(key, 0) > 0:
                self.not_ready[key] -= 1
                return httpx.Response(404)
            if key not in self.files:
                return httpx.Response(404)
            name, data = self.files[key]
            escaped = "".join(f"\\u{ord(c):04x}" if ord(c) > 127 else c for c in name)
            return httpx.Response(200, content=data, headers={"Content-Disposition": escaped})
        if request.method == "GET" and path.startswith("/user/"):
            user = int(path.rsplit("/", 1)[1])
            if user not in self.cards:
                return httpx.Response(404)
            return httpx.Response(200, json=self.cards[user])
        if request.method == "GET" and path == "/users":
            return httpx.Response(200, json=list(self.cards.values()))
        if request.method == "POST" and path == "/message/history":
            body = json.loads(request.content)
            items = [
                m for m in self.history.get(body["targetId"], []) if m["ctime"] > body["cTime"]
            ]
            return httpx.Response(200, json=items[: body["messageCount"]])
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeStream:
    """WebSocket-сервер /notify: принимает CONNECT и SUBSCRIBE, рассылает события MESSAGE."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.clients: list[Any] = []
        self.subscribed = asyncio.Event()
        self._server: Any = None
        self.port = 0

    async def start(self) -> None:
        self._server = await websockets.serve(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _client(self, socket: Any) -> None:
        self.clients.append(socket)
        try:
            async for message in socket:
                self.received.append(message)
                if message.startswith("SUBSCRIBE"):
                    self.subscribed.set()
        except websockets.ConnectionClosed:
            pass

    async def publish(self, event: dict[str, Any]) -> None:
        frame = stomp.frame(
            "MESSAGE",
            ("destination", "/topic/messages"),
            ("subscription", "sub-0"),
            body=json.dumps(event, ensure_ascii=False),
        )
        for socket in self.clients:
            await socket.send(frame)

    async def drop(self) -> None:
        for socket in self.clients:
            await socket.close()
        self.clients.clear()
        self.subscribed.clear()
