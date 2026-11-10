"""Команды: русскими словами без косой черты, первым словом сообщения.

Все администраторы равны. Назначает и снимает администраторов только владелец (OWNER_IDS из
`.env`), владельца командой бота не снять.
"""

import csv
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from doc_registry_bot.bot import pending
from doc_registry_bot.bot.context import Ctx
from doc_registry_bot.bot.people import (
    Person,
    Role,
    as_dict,
    from_dict,
    members,
    person_of,
    remember,
    resolve,
    tokens_of,
    unique,
)
from doc_registry_bot.bot.registration import refused, refused_headed
from doc_registry_bot.bot.types import Attachment, MessengerError
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import AuditEntry, Member
from doc_registry_bot.registry import instructions, markup, schema, text
from doc_registry_bot.registry.markup import bold, escape

HELP = "помощь"
INSTRUCTION = "инструкция"
GRANT = "доступ"
REVOKE = "забрать доступ"
USERS = "пользователи"
EXPORT = "выгрузка"
SEND = "отправить"
AUDIT = "журнал действий"
ADMIN = "админ"
UNADMIN = "снять админа"
ADMINS = "админы"

# Длинные раньше коротких: «админы» не должно читаться как «админ» с аргументом «ы».
_ORDER = (REVOKE, AUDIT, UNADMIN, ADMINS, ADMIN, GRANT, USERS, EXPORT, SEND, HELP, INSTRUCTION)
_ADMIN_ONLY = {GRANT, REVOKE, USERS, EXPORT, SEND, AUDIT}
_OWNER_ONLY = {ADMIN, UNADMIN, ADMINS}

NO_RIGHTS = "Нет прав на эту команду. Список ваших команд: Помощь"
DENIED = "У вас нет доступа к боту регистрации КД. Обратитесь к администратору."
EVERYONE_WORDS = ("всем", "все", "всем с доступом", "все с доступом")
GRANT_PROMPT = (
    "Ответьте: <b>Да</b> выдать доступ и отправить инструкцию, <b>Без инструкции</b> только "
    "выдать доступ, <b>Нет</b> отмена."
)


@dataclass(frozen=True)
class Command:
    word: str
    argument: str
    body: str


def parse(plain: str) -> Command | None:
    """Команда по первой строке сообщения; None — это не команда."""
    first, _, rest = plain.strip().partition("\n")
    head = text.fold(first)
    for word in _ORDER:
        if head == word or head.startswith((word + " ", word + ":")):
            argument = first.strip()[len(word) :].strip().lstrip(":").strip()
            return Command(word, argument, rest.strip())
    return None


def allowed(command: Command, role: Role) -> bool:
    if command.word in _OWNER_ONLY:
        return role.owner
    if command.word in _ADMIN_ONLY:
        return role.is_admin
    return role.can_register


async def run(ctx: Ctx, command: Command, role: Role, file_ids: list[int], message_id: int) -> None:
    if not allowed(command, role):
        refused(ctx, NO_RIGHTS)
        return
    handler = _HANDLERS[command.word]
    await handler(ctx, command, role, file_ids, message_id)


# ── Помощь и инструкция ───────────────────────────────────────────────────────


def help_text(role: Role) -> str:
    lines = [
        bold("Команды"),
        "Регистрация, Просмотр, Удаление, Изменение: по образцам из Инструкции",
        "Инструкция: правила и примеры сообщений",
        "Помощь: этот список",
    ]
    if role.is_admin:
        lines += [
            "",
            bold("Администратору"),
            "Доступ ID или ФИО: выдать доступ, можно несколько через запятую",
            "Забрать доступ ID или ФИО: снять доступ",
            "Пользователи: у кого есть доступ",
            "Выгрузка: журнал КД файлом CSV",
            "Отправить: получатели в первой строке, текст со второй, можно приложить файлы",
            "Журнал действий: кто что делал, файлом CSV",
        ]
    if role.owner:
        lines += [
            "",
            bold("Владельцу"),
            "Админ ID или ФИО: назначить администратора",
            "Снять админа ID или ФИО: снять администратора",
            "Админы: список администраторов",
        ]
    return "\n".join(lines)


async def _help(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    ctx.reply(help_text(role))


async def _instruction(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    ctx.reply(instructions.TEXT)


# ── Выбор людей ───────────────────────────────────────────────────────────────


async def _people_or_ask(
    ctx: Ctx, command: Command, example: str, extra: dict[str, Any] | None = None
) -> list[Person] | None:
    """Люди из аргумента команды. None — ответ уже дан: ошибка или вопрос «кого именно»."""
    tokens = tokens_of(command.argument)
    if not tokens:
        refused(
            ctx,
            f"{command.argument or _title(command.word)}: укажите ID или ФИО (Пример: {example})",
        )
        return None
    try:
        found = await resolve(ctx, tokens)
    except MessengerError:
        refused(ctx, "Мессенджер: не удалось получить список контактов. Повторите позже")
        return None
    if found.missing:
        refused(ctx, "\n".join(f"{name}: не найден среди контактов бота" for name in found.missing))
        return None
    if found.ambiguous:
        candidates: list[dict[str, object]] = []
        lines = [bold("Найдено несколько человек. Ответьте номерами через запятую:")]
        for token, people in found.ambiguous:
            lines.append(f"По запросу «{escape(token)}»:")
            for person in people:
                candidates.append(as_dict(person))
                lines.append(f"{len(candidates)}. {escape(person.label())}")
        lines.append("Для отмены ответьте Нет")
        await pending.put(
            ctx,
            "choose",
            {
                "command": command.word,
                "argument": command.argument,
                "body": command.body,
                "found": [as_dict(p) for p in found.found],
                "candidates": candidates,
                **(extra or {}),
            },
        )
        ctx.reply("\n".join(lines))
        return None
    return unique(found.found)


def parse_choice(plain: str) -> list[int] | None:
    """Номера через запятую или пробел; None — ответ не номера."""
    parts = [part for part in plain.replace(",", " ").replace(";", " ").split() if part]
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return [int(part) for part in parts]


def choice_problem(payload: dict[str, Any], numbers: list[int]) -> str | None:
    count = len(payload["candidates"])
    if any(not 1 <= number <= count for number in numbers):
        return f"Выбор: номера должны быть от 1 до {count}"
    return None


async def apply_choice(ctx: Ctx, role: Role, payload: dict[str, Any], numbers: list[int]) -> None:
    """Продолжить команду с выбранными людьми."""
    candidates = [from_dict(item) for item in payload["candidates"]]
    found = [from_dict(item) for item in payload["found"]]
    people = unique(found + [candidates[number - 1] for number in numbers])
    command = Command(payload["command"], payload["argument"], payload["body"])
    if not allowed(command, role):
        refused(ctx, NO_RIGHTS)
        return
    await _continue(ctx, command, people, payload)


async def _continue(
    ctx: Ctx, command: Command, people: list[Person], payload: dict[str, Any]
) -> None:
    if command.word == GRANT:
        await _ask_grant(ctx, people)
    elif command.word == REVOKE:
        await _revoke_people(ctx, people)
    elif command.word == ADMIN:
        await _admin_people(ctx, people)
    elif command.word == UNADMIN:
        await _unadmin_people(ctx, people)
    elif command.word == SEND:
        await _send_or_ask(ctx, people, command.body, payload["message_id"], payload["file_ids"])


def _title(word: str) -> str:
    return word[:1].upper() + word[1:]


def _list(people: list[Person]) -> str:
    return "\n".join(f"{index}. {escape(p.label())}" for index, p in enumerate(people, start=1))


# ── Доступ ────────────────────────────────────────────────────────────────────


async def _grant(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    people = await _people_or_ask(ctx, command, "Доступ 123456, Иванов Иван Иванович")
    if people is not None:
        await _ask_grant(ctx, people)


async def _ask_grant(ctx: Ctx, people: list[Person]) -> None:
    await pending.put(ctx, "grant", {"people": [as_dict(p) for p in people]})
    ctx.reply(
        bold("Выдать доступ к регистрации КД:")
        + "\n"
        + _list(people)
        + "\n\n"
        + GRANT_PROMPT
        + "\n"
        + markup.LIFETIME
    )


async def confirm_grant(ctx: Ctx, payload: dict[str, Any], with_instruction: bool) -> None:
    people = [from_dict(item) for item in payload["people"]]
    for person in people:
        member = await remember(ctx, person)
        member.has_access = True
    names = ", ".join(p.label() for p in people)
    ctx.audit("Выдача доступа", names + ("" if with_instruction else " (без инструкции)"))
    if with_instruction:
        for person in people:
            ctx.send_to(person.user_id, instructions.GREETING)
    tail = "Инструкция отправлена." if with_instruction else "Инструкция не отправлялась."
    ctx.reply(bold(f"Доступ выдан: {len(people)}") + "\n" + _list(people) + "\n" + tail)


async def _revoke(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    people = await _people_or_ask(ctx, command, "Забрать доступ 123456")
    if people is not None:
        await _revoke_people(ctx, people)


async def _revoke_people(ctx: Ctx, people: list[Person]) -> None:
    notes: list[str] = []
    for person in people:
        member = await remember(ctx, person)
        member.has_access = False
        if person.user_id in ctx.config.owner_ids:
            notes.append(f"{person.fio}: владелец, доступ задан в настройках сервера")
        elif member.is_admin:
            notes.append(
                f"{person.fio}: администратор, регистрировать может, пока он администратор"
            )
    ctx.audit("Снятие доступа", ", ".join(p.label() for p in people))
    reply = bold(f"Доступ снят: {len(people)}") + "\n" + _list(people)
    if notes:
        reply += "\n" + "\n".join(escape(note) for note in notes)
    ctx.reply(reply)


async def _users(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    rows = [m for m in await members(ctx) if m.has_access or m.is_admin]
    known = {m.user_id for m in rows}
    owners = [owner for owner in ctx.config.owner_ids if owner not in known]
    lines = [bold(f"С доступом к регистрации: {len(rows) + len(owners)}")]
    number = 0
    for owner in owners:
        number += 1
        lines.append(f"{number}. ID {owner}, владелец")
    for member in rows:
        number += 1
        marks = []
        if member.user_id in ctx.config.owner_ids:
            marks.append("владелец")
        elif member.is_admin:
            marks.append("администратор")
        suffix = (", " + ", ".join(marks)) if marks else ""
        lines.append(
            f"{number}. {escape(member.fio or 'ФИО не заполнено')}, ID {member.user_id}{suffix}"
        )
    ctx.reply("\n".join(lines))


# ── Администраторы ────────────────────────────────────────────────────────────


async def _admin(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    people = await _people_or_ask(ctx, command, "Админ 123456")
    if people is not None:
        await _admin_people(ctx, people)


async def _admin_people(ctx: Ctx, people: list[Person]) -> None:
    for person in people:
        member = await remember(ctx, person)
        member.is_admin = True
        ctx.send_to(
            person.user_id,
            "Вам выданы права администратора бота регистрации КД. Список команд: Помощь",
        )
    ctx.audit("Назначение администраторов", ", ".join(p.label() for p in people))
    ctx.reply(bold(f"Назначены администраторами: {len(people)}") + "\n" + _list(people))


async def _unadmin(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    people = await _people_or_ask(ctx, command, "Снять админа 123456")
    if people is not None:
        await _unadmin_people(ctx, people)


async def _unadmin_people(ctx: Ctx, people: list[Person]) -> None:
    owners = [p for p in people if p.user_id in ctx.config.owner_ids]
    if owners:
        refused(
            ctx,
            "\n".join(
                f"{p.fio or p.user_id}: владельца снять нельзя, он задан в настройках сервера"
                for p in owners
            ),
        )
        return
    for person in people:
        member = await remember(ctx, person)
        member.is_admin = False
    ctx.audit("Снятие администраторов", ", ".join(p.label() for p in people))
    ctx.reply(bold(f"Сняты с администраторов: {len(people)}") + "\n" + _list(people))


async def _admins(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    admins = [m for m in await members(ctx) if m.is_admin]
    lines = [bold("Владельцы")]
    lines += [f"ID {owner}" for owner in ctx.config.owner_ids]
    lines.append(bold(f"Администраторы: {len(admins)}"))
    lines += [
        f"{index}. {escape(m.fio or 'ФИО не заполнено')}, ID {m.user_id}"
        for index, m in enumerate(admins, start=1)
    ]
    ctx.reply("\n".join(lines))


# ── Выгрузки ──────────────────────────────────────────────────────────────────


def _csv(header: list[str], rows: list[list[str]]) -> bytes:
    """CSV для табличного редактора: UTF-8 с меткой и разделитель «;»."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


async def journal_csv(ctx: Ctx) -> bytes:
    entries = await journal.all_entries(ctx.session)
    return _csv(
        list(schema.COLUMNS),
        [
            [
                e.name,
                e.designation,
                e.fio,
                ctx.local(e.registered_at),
                e.project,
                e.accounting,
                e.first_use,
                e.summary,
            ]
            for e in entries
        ],
    )


async def _export(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    data = await journal_csv(ctx)
    count = await journal.count(ctx.session)
    stamp = ctx.now.astimezone(ctx.config.zone).strftime("%Y-%m-%d")
    ctx.audit("Выгрузка журнала", f"записей: {count}")
    ctx.reply(
        bold("Выгрузка журнала КД") + f"\nЗаписей: {count}",
        [Attachment(f"Журнал КД {stamp}.csv", data)],
    )


async def _audit(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    since = ctx.now - timedelta(days=366)
    rows = list(
        await ctx.session.scalars(
            select(AuditEntry).where(AuditEntry.at >= since).order_by(AuditEntry.at)
        )
    )
    data = _csv(
        ["Когда", "Кто", "ID", "Действие", "Подробности"],
        [[ctx.local(r.at), r.actor_fio, str(r.actor_id), r.action, r.details] for r in rows],
    )
    last = rows[-10:]
    lines = [bold(f"Журнал действий: {len(rows)} записей за год")]
    lines += [
        f"{ctx.local(r.at)} {escape(r.actor_fio or str(r.actor_id))}: {escape(r.action)}"
        for r in reversed(last)
    ]
    stamp = ctx.now.astimezone(ctx.config.zone).strftime("%Y-%m-%d")
    ctx.reply("\n".join(lines), [Attachment(f"Журнал действий {stamp}.csv", data)])


# ── Отправка сообщений ────────────────────────────────────────────────────────


async def _send(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    if not command.argument:
        refused(
            ctx,
            "Отправить: укажите получателей после слова Отправить "
            "(Пример: Отправить: Иванов Иван Иванович, 123456)",
        )
        return
    if not command.body:
        refused(ctx, "Отправить: нет текста сообщения. Напишите текст со второй строки")
        return
    if text.fold(command.argument) in EVERYONE_WORDS:
        everyone = [m for m in await members(ctx) if (m.has_access or m.is_admin)]
        people = [Person(m.user_id, m.fio) for m in everyone if m.user_id != ctx.sender_id]
        people += [
            Person(owner, "")
            for owner in ctx.config.owner_ids
            if owner != ctx.sender_id and owner not in {p.user_id for p in people}
        ]
        if not people:
            refused(ctx, "Отправить: получателей нет")
            return
        await _send_or_ask(ctx, people, command.body, message_id, files)
        return
    chosen = await _people_or_ask(
        ctx,
        command,
        "Отправить: Иванов Иван Иванович, 123456",
        {"message_id": message_id, "file_ids": files},
    )
    if chosen is not None:
        await _send_or_ask(ctx, chosen, command.body, message_id, files)


async def _send_or_ask(
    ctx: Ctx, people: list[Person], body: str, message_id: int, file_ids: list[int]
) -> None:
    if len(people) == 1:
        await deliver(ctx, people, body, message_id, file_ids)
        return
    await pending.put(
        ctx,
        "send",
        {
            "people": [as_dict(p) for p in people],
            "body": body,
            "message_id": message_id,
            "file_ids": file_ids,
        },
    )
    shown = people[:30]
    names = _list(shown) + (f"\nи еще {len(people) - len(shown)}" if len(people) > 30 else "")
    files = f"\nФайлов: {len(file_ids)}" if file_ids else ""
    ctx.reply(
        bold(f"Отправить сообщение получателям: {len(people)}")
        + "\n"
        + names
        + "\n\n"
        + bold("Текст:")
        + "\n"
        + escape(body)
        + files
        + "\n\n"
        + markup.PROMPT
    )


async def deliver(
    ctx: Ctx, people: list[Person], body: str, message_id: int, file_ids: list[int]
) -> None:
    """Отправить сразу, не дожидаясь коммита: человеку нужен итог, сколько дошло."""
    try:
        attachments = [await ctx.messenger.download(message_id, f) for f in file_ids]
    except MessengerError as error:
        refused(ctx, f"Файлы: не удалось получить вложения ({error}). Отправьте сообщение заново")
        return
    html = escape(body)
    failed: list[str] = []
    for person in people:
        try:
            await ctx.messenger.send(person.user_id, html, attachments)
        except MessengerError as error:
            failed.append(f"{person.label()}: {error}")
    sent = len(people) - len(failed)
    ctx.audit(
        "Отправка сообщения",
        f"получателей: {len(people)}, доставлено: {sent}, файлов: {len(attachments)}",
    )
    reply = bold(f"Отправлено: {sent} из {len(people)}")
    if failed:
        reply += "\n" + bold("Не отправлено:") + "\n" + "\n".join(escape(f) for f in failed)
    ctx.reply(reply)


async def confirm_send(ctx: Ctx, payload: dict[str, Any]) -> None:
    people = [from_dict(item) for item in payload["people"]]
    await deliver(ctx, people, payload["body"], payload["message_id"], payload["file_ids"])


def pending_refusal(ctx: Ctx, kind: str, payload: dict[str, Any]) -> None:
    """Пока ждем ответа, новую операцию начать нельзя."""
    if kind in ("delete", "edit"):
        action = "Удаление" if kind == "delete" else "Изменение"
        number = payload["card"]["number"]
        refused(
            ctx,
            f"Ожидается подтверждение: {action}, {number}. Подтвердите: Да или Нет. "
            "Для нового запроса сначала ответьте Нет",
        )
    elif kind == "choose":
        refused(ctx, "Ожидается выбор: ответьте номерами из списка через запятую или Нет")
    elif kind == "grant":
        refused(ctx, "Ожидается ответ: Да, Без инструкции или Нет")
    else:
        refused_headed(ctx, "Ожидается подтверждение. Для нового запроса сначала ответьте Нет")


Handler = Callable[[Ctx, Command, Role, list[int], int], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {
    HELP: _help,
    INSTRUCTION: _instruction,
    GRANT: _grant,
    REVOKE: _revoke,
    USERS: _users,
    EXPORT: _export,
    SEND: _send,
    AUDIT: _audit,
    ADMIN: _admin,
    UNADMIN: _unadmin,
    ADMINS: _admins,
}


def card_person(member: Member) -> Person:
    return Person(member.user_id, member.fio)


__all__ = ["DENIED", "Command", "apply_choice", "parse", "person_of", "run"]
