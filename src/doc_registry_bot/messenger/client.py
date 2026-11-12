"""HTTP API службы мессенджера: ответы, файлы, карточки, контакты, история.

Ответ уходит запросом `PUT /message` с `targetType: person`, перенос строки в тексте — `<br>`.
Файлы: сообщение создается со списком файлов, затем каждый файл загружается блоками по 1 МиБ
(`POST /message/file`, заголовок `Upload-Offset`).
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import unquote

import httpx

from doc_registry_bot.bot.types import Attachment, HistoryMessage, MessengerError, UserCard

log = logging.getLogger(__name__)

BLOCK_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
DOWNLOAD_WAIT_SECONDS = 30.0
_RETRIES = 3


class HttpMessenger:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(30.0), transport=transport
        )
        self.download_wait = DOWNLOAD_WAIT_SECONDS
        self.download_pause = 2.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Запрос с повтором при обрыве связи (не при ответе сервера с ошибкой)."""
        last: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                return await self._client.request(method, url, **kwargs)
            except httpx.TransportError as error:
                last = error
                await asyncio.sleep(0.5 * (attempt + 1))
        raise MessengerError(f"служба мессенджера недоступна: {last}") from last

    @staticmethod
    def _fail(what: str, response: httpx.Response) -> MessengerError:
        detail = response.text.strip()[:200]
        return MessengerError(f"{what}: код {response.status_code} {detail}".strip())

    # ── Сообщения ─────────────────────────────────────────────────────────────

    async def send(self, user_id: int, html: str, files: list[Attachment] | None = None) -> None:
        files = files or []
        if any(not file.data for file in files):
            raise MessengerError("пустой файл передать нельзя")
        ids = [str(uuid.uuid4()) for _ in files]
        body: dict[str, Any] = {
            "targetId": user_id,
            "targetType": "person",
            "message": html.replace("\n", "<br>"),
        }
        if files:
            body["files"] = [
                {"fileUuid": ids[i], "length": len(f.data), "name": f.name, "directory": False}
                for i, f in enumerate(files)
            ]
        response = await self._request("PUT", "/message", json=body)
        if response.status_code == 404:
            # По описанию API: «Контакта нет в списке пользователей».
            raise MessengerError("бота нет в контактах получателя")
        if response.status_code != 200:
            raise self._fail("отправка сообщения", response)
        if not files:
            return
        try:
            message = str(response.json()["uuid"])
        except (ValueError, KeyError, TypeError) as error:
            raise MessengerError("служба не вернула идентификатор сообщения") from error
        for file_id, file in zip(ids, files, strict=True):
            for offset in range(0, len(file.data), BLOCK_BYTES):
                block = file.data[offset : offset + BLOCK_BYTES]
                uploaded = await self._request(
                    "POST",
                    "/message/file",
                    params={"msgUuid": message, "fileUuid": file_id},
                    headers={
                        "Upload-Offset": str(offset),
                        "Content-Type": "application/octet-stream",
                    },
                    content=block,
                )
                if uploaded.status_code != 200:
                    raise self._fail(f"загрузка файла «{file.name}», смещение {offset}", uploaded)

    async def download(self, message_id: int, file_id: int) -> Attachment:
        """Скачать вложение. Пока сервер готовит файл (404), ждем до 30 секунд."""
        waited = 0.0
        while True:
            response = await self._request(
                "POST",
                "/message/download",
                json={"serverMsgId": message_id, "serverFileId": file_id},
                headers={"Accept": "application/octet-stream"},
            )
            if response.status_code == 200:
                break
            if response.status_code != 404 or waited >= self.download_wait:
                raise self._fail("скачивание файла", response)
            await asyncio.sleep(self.download_pause)
            waited += self.download_pause
        if len(response.content) > MAX_DOWNLOAD_BYTES:
            raise MessengerError("файл больше 50 МБ")
        name = file_name(response.headers.get("content-disposition"))
        return Attachment(name, response.content)

    # ── Люди ──────────────────────────────────────────────────────────────────

    async def user_card(self, user_id: int) -> UserCard | None:
        response = await self._request("GET", f"/user/{user_id}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._fail("карточка пользователя", response)
        return card_of(response.json())

    async def me(self) -> UserCard | None:
        response = await self._request("GET", "/user")
        if response.status_code != 200:
            raise self._fail("карточка бота", response)
        return card_of(response.json())

    async def contacts(self) -> list[UserCard]:
        response = await self._request("GET", "/users")
        if response.status_code != 200:
            raise self._fail("список контактов", response)
        data = response.json()
        cards = [card_of(item) for item in data] if isinstance(data, list) else []
        return [card for card in cards if card is not None]

    async def history(self, user_id: int, after: int, limit: int) -> list[HistoryMessage]:
        response = await self._request(
            "POST",
            "/message/history",
            json={
                "targetId": user_id,
                "targetType": "person",
                "cTime": after,
                "messageCount": max(1, min(limit, 100)),
                "getPreview": False,
            },
        )
        if response.status_code != 200:
            raise self._fail("история сообщений", response)
        data = response.json()
        items = [history_of(item) for item in data] if isinstance(data, list) else []
        return sorted((item for item in items if item is not None), key=lambda m: m.ctime)


# ── Разбор ответов службы ─────────────────────────────────────────────────────


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return str(value).strip() if isinstance(value, str | int) else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def card_of(data: Any) -> UserCard | None:
    if not isinstance(data, dict):
        return None
    user_id = _int(data.get("userId"))
    if user_id is None or user_id <= 0:
        return None
    return UserCard(
        user_id,
        _text(data, "family"),
        _text(data, "fname"),
        _text(data, "sname"),
        _text(data, "division"),
        _text(data, "post"),
    )


def file_ids_of(value: Any) -> list[int]:
    """Идентификаторы файлов: список чисел, число или строка вида «[1, 2]»."""
    if isinstance(value, list):
        return [n for n in (_int(item) for item in value) if n is not None]
    single = _int(value)
    if single is not None:
        return [single]
    if isinstance(value, str):
        return [int(part) for part in re.findall(r"\d+", value)]
    return []


def history_of(data: Any) -> HistoryMessage | None:
    if not isinstance(data, dict):
        return None
    message_id = _int(data.get("serverMessageId")) or _int(data.get("id"))
    owner = _int(data.get("ownerId"))
    ctime = _int(data.get("ctime"))
    if message_id is None or owner is None or ctime is None:
        return None
    files = file_ids_of(data.get("serverFileIds")) if data.get("hasFile") else []
    return HistoryMessage(message_id, owner, str(data.get("text") or ""), ctime, files)


_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_FORBIDDEN = set('/\\<>:"|?*')


def _usable(name: str) -> str:
    name = name.strip()
    if (
        not name
        or len(name) > 240
        or name in (".", "..")
        or name.endswith((".", " "))
        or any(ord(c) < 32 or c in _FORBIDDEN or c == "�" for c in name)
    ):
        return ""
    return name


def file_name(header: str | None) -> str:
    """Имя файла из Content-Disposition: filename*, filename или голое имя с экранами \\uXXXX.

    Служба мессенджера кодирует русские буквы в имени экранами \\uXXXX.
    """
    if not header:
        return ""
    star = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", header, re.IGNORECASE)
    if star:
        try:
            return _usable(unquote(star.group(2).strip(), encoding=star.group(1) or "utf-8"))
        except LookupError:
            return ""
    plain = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"|filename\s*=\s*([^;]+)', header, re.I)
    if plain:
        raw = plain.group(1) if plain.group(1) is not None else plain.group(2)
        return _usable(_unescape(raw))
    if ";" in header or "," in header:
        return ""
    value = header.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    decoded = _unescape(value)
    if decoded == value and "." not in value:
        return ""
    return _usable(decoded)


def _unescape(value: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), value)


def event_items(body: str) -> tuple[str, list[dict[str, Any]]]:
    """Тело события потока: тип и строки из `data`."""
    try:
        data = json.loads(body)
    except ValueError:
        return "", []
    if not isinstance(data, dict):
        return "", []
    rows: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        # Служба присылает строки и списком, и списком списков: "data": [[{...}]].
        if isinstance(value, dict):
            rows.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(data.get("data"))
    return str(data.get("type") or ""), rows
