"""Команды: русскими словами без косой черты, в начале сообщения.

Все администраторы равны. Назначает и снимает администраторов только владелец (OWNER_IDS из
`.env`), владельца командой бота не снять. Людей в командах указывают только полным ФИО или ID,
и перед любой командой о людях бот показывает, кого нашел, и ждет подтверждения.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from doc_registry_bot import importer
from doc_registry_bot.bot import copies, pending
from doc_registry_bot.bot.context import Ctx
from doc_registry_bot.bot.people import (
    Person,
    Role,
    as_dict,
    from_dict,
    members,
    remember,
    resolve,
    role_of,
    tokens_of,
    unique,
)
from doc_registry_bot.bot.registration import refused, refused_headed
from doc_registry_bot.bot.types import Attachment, MessengerError
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import AuditEntry
from doc_registry_bot.registry import instructions, markup, text
from doc_registry_bot.registry.markup import bold, escape

HELP = "помощь"
INSTRUCTION = "инструкция"
ADMIN_INSTRUCTION = "инструкция администратора"
OWNER_INSTRUCTION = "инструкция главного администратора"
BACKUP = copies.BACKUP
REPLACE = copies.REPLACE
GRANT = "выдать доступ"
REVOKE = "удалить доступ"
USERS = "пользователи"
EXPORT = "выгрузить журнал"
AUDIT = "журнал действий"
SEND = "отправить сообщение"
SEND_INSTRUCTION = "отправить инструкцию"
ADMIN = "назначить администратора"
UNADMIN = "снять администратора"
ADMINS = "администраторы"

# Длинные раньше коротких: «инструкция администратора» не должна читаться как «инструкция».
_ORDER = (
    OWNER_INSTRUCTION,
    ADMIN_INSTRUCTION,
    INSTRUCTION,
    GRANT,
    REVOKE,
    USERS,
    EXPORT,
    AUDIT,
    SEND_INSTRUCTION,
    SEND,
    ADMIN,
    UNADMIN,
    ADMINS,
    BACKUP,
    REPLACE,
    HELP,
)
_ADMIN_ONLY = {ADMIN_INSTRUCTION, GRANT, REVOKE, USERS, EXPORT, AUDIT, SEND, SEND_INSTRUCTION}
# Главный администратор (владелец из OWNER_IDS): администраторы, резервная копия, замена журнала.
_OWNER_ONLY = {ADMIN, UNADMIN, ADMINS, OWNER_INSTRUCTION, BACKUP, REPLACE}

# Доступны и тогда, когда бот ждет ответа на подтверждение.
ALWAYS = (HELP, INSTRUCTION, ADMIN_INSTRUCTION, OWNER_INSTRUCTION)

# Команды о людях: каждая ждет подтверждения, вид ожидания — слово команды.
PEOPLE_COMMANDS = (GRANT, REVOKE, SEND, SEND_INSTRUCTION, ADMIN, UNADMIN)

NO_RIGHTS = "Нет прав на эту команду. Список ваших команд: Помощь"
DENIED = "У вас нет доступа к боту регистрации КД. Обратитесь к администратору."
EVERYONE_WORDS = ("всем", "все", "всем с доступом", "все с доступом")
MESSAGE_KEY = "Сообщение"
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


def allowed(word: str, role: Role) -> bool:
    if word in _OWNER_ONLY:
        return role.owner
    if word in _ADMIN_ONLY:
        return role.is_admin
    return role.can_register


async def run(ctx: Ctx, command: Command, role: Role, file_ids: list[int], message_id: int) -> None:
    if not allowed(command.word, role):
        refused(ctx, NO_RIGHTS)
        return
    handler = _HANDLERS[command.word]
    await handler(ctx, command, role, file_ids, message_id)


# ── Помощь и инструкции ───────────────────────────────────────────────────────


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
            "Инструкция администратора: как пользоваться командами ниже",
            "Выдать доступ: полное ФИО или ID, через запятую",
            "Удалить доступ: полное ФИО или ID",
            "Отправить инструкцию: полное ФИО или ID",
            "Пользователи: у кого есть доступ",
            "Выгрузить журнал: журнал КД файлом CSV",
            "Журнал действий: кто что делал, файлом CSV",
            "Отправить сообщение: получатели, со следующей строки Сообщение: текст",
        ]
    if role.owner:
        lines += [
            "",
            bold("Главному администратору"),
            "Инструкция главного администратора: как пользоваться командами ниже",
            "Назначить администратора: полное ФИО или ID",
            "Снять администратора: полное ФИО или ID",
            "Администраторы: список",
            "Резервная копия: файл базы и журнал КД в выбранную папку",
            "Заменить журнал: весь журнал КД из файла CSV во вложении",
        ]
    return "\n".join(lines)


async def _help(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    ctx.reply(help_text(role))


async def _instruction(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    ctx.reply(instructions.TEXT)


async def _admin_instruction(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    ctx.reply(instructions.ADMIN_TEXT)


async def _owner_instruction(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    ctx.reply(instructions.OWNER_TEXT)


async def _backup(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    await copies.backup_command(ctx, command.argument)


async def _replace(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    await copies.replace_command(ctx, files, message_id)


# ── Люди: поиск и подтверждение ───────────────────────────────────────────────


def _title(word: str) -> str:
    return word[:1].upper() + word[1:]


def _list(people: list[Person]) -> str:
    return "\n".join(f"{index}. {escape(p.label())}" for index, p in enumerate(people, start=1))


async def _people(ctx: Ctx, command: Command, argument: str) -> list[Person] | None:
    """Люди из команды. None — ответ уже дан: ошибка в списке людей."""
    tokens = tokens_of(argument)
    if not tokens:
        refused(
            ctx,
            f"{_title(command.word)}: укажите полное ФИО или ID "
            f"(Пример: {_title(command.word)}: Иванов Иван Иванович, 123456)",
        )
        return None
    try:
        found = await resolve(ctx, tokens)
    except MessengerError:
        refused(ctx, "Мессенджер: не удалось найти людей. Повторите позже")
        return None
    if found.problems:
        refused(ctx, "\n".join(found.problems))
        return None
    return unique(found.found)


_QUESTIONS = {
    REVOKE: "Удалить доступ к регистрации КД:",
    SEND_INSTRUCTION: "Отправить инструкцию:",
    ADMIN: "Назначить администраторами:",
    UNADMIN: "Снять с администраторов:",
}


async def _ask(
    ctx: Ctx, word: str, people: list[Person], extra: dict[str, Any] | None = None
) -> None:
    """Показать найденных и ждать подтверждения."""
    await pending.put(ctx, word, {"people": [as_dict(p) for p in people], **(extra or {})})
    if word == GRANT:
        ctx.reply(
            bold("Выдать доступ к регистрации КД:")
            + "\n"
            + _list(people)
            + "\n\n"
            + GRANT_PROMPT
            + "\n"
            + markup.LIFETIME
        )
        return
    ctx.reply(bold(_QUESTIONS[word]) + "\n" + _list(people) + "\n\n" + markup.PROMPT)


async def _people_command(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    people = await _people(ctx, command, command.argument)
    if people is None:
        return
    if command.word == UNADMIN:
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
    if command.word == SEND_INSTRUCTION:
        without = [p for p in people if not (await role_of(ctx, p.user_id)).can_register]
        if without:
            refused(
                ctx,
                "\n".join(
                    f"{p.fio or p.user_id}: нет доступа к боту, сначала Выдать доступ"
                    for p in without
                ),
            )
            return
    await _ask(ctx, command.word, people)


async def confirm(ctx: Ctx, word: str, payload: dict[str, Any], answer: str) -> None:
    """Ответ на подтверждение команды о людях. `answer`: «да», «без инструкции» или «нет»."""
    people = [from_dict(item) for item in payload["people"]]
    if answer == "нет":
        ctx.reply(bold("Операция отменена"))
        return
    if word == GRANT:
        await _grant(ctx, people, with_instruction=answer == "да")
    elif word == REVOKE:
        await _revoke(ctx, people)
    elif word == SEND_INSTRUCTION:
        failed = await _deliver_instruction(ctx, people)
        ctx.audit("Отправка инструкции", ", ".join(p.label() for p in people))
        ctx.reply(_delivery_report("Инструкция отправлена", len(people), failed))
    elif word == ADMIN:
        await _admin(ctx, people)
    elif word == UNADMIN:
        await _unadmin(ctx, people)
    elif word == SEND:
        await _deliver_message(
            ctx, people, payload["body"], payload["message_id"], payload["file_ids"]
        )


# ── Доступ ────────────────────────────────────────────────────────────────────


async def _deliver_instruction(ctx: Ctx, people: list[Person]) -> list[str]:
    """Отправить инструкцию сразу и узнать, кому не дошла."""
    failed: list[str] = []
    for person in people:
        try:
            await ctx.messenger.send(person.user_id, instructions.GREETING)
        except MessengerError as error:
            failed.append(f"{person.label()}: {error}")
    return failed


def _delivery_report(heading: str, total: int, failed: list[str]) -> str:
    report = bold(f"{heading}: {total - len(failed)} из {total}")
    if failed:
        report += "\n" + bold("Не дошла:") + "\n" + "\n".join(escape(item) for item in failed)
    return report


async def _grant(ctx: Ctx, people: list[Person], with_instruction: bool) -> None:
    for person in people:
        member = await remember(ctx, person)
        member.has_access = True
    names = ", ".join(p.label() for p in people)
    ctx.audit("Выдача доступа", names + ("" if with_instruction else " (без инструкции)"))
    reply = bold(f"Доступ выдан: {len(people)}") + "\n" + _list(people)
    if with_instruction:
        failed = await _deliver_instruction(ctx, people)
        reply += "\n" + _delivery_report("Инструкция отправлена", len(people), failed)
    else:
        reply += "\nИнструкция не отправлялась."
    ctx.reply(reply)


async def _revoke(ctx: Ctx, people: list[Person]) -> None:
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
    ctx.audit("Удаление доступа", ", ".join(p.label() for p in people))
    reply = bold(f"Доступ удален: {len(people)}") + "\n" + _list(people)
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
        name = escape(member.fio or "ФИО не заполнено")
        lines.append(f"{number}. {name}, ID {member.user_id}{suffix}")
    ctx.reply("\n".join(lines))


# ── Администраторы ────────────────────────────────────────────────────────────


async def _admin(ctx: Ctx, people: list[Person]) -> None:
    for person in people:
        member = await remember(ctx, person)
        member.is_admin = True
        ctx.send_to(
            person.user_id,
            "Вам выданы права администратора бота регистрации КД. "
            "Как ими пользоваться: Инструкция администратора",
        )
    ctx.audit("Назначение администраторов", ", ".join(p.label() for p in people))
    ctx.reply(bold(f"Назначены администраторами: {len(people)}") + "\n" + _list(people))


async def _unadmin(ctx: Ctx, people: list[Person]) -> None:
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


async def _export(
    ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int
) -> None:
    data = await importer.export_csv(ctx.session, ctx.config.zone)
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
    data = importer.csv_bytes(
        ["Когда", "Кто", "ID", "Действие", "Подробности"],
        [[ctx.local(r.at), r.actor_fio, str(r.actor_id), r.action, r.details] for r in rows],
    )
    lines = [bold(f"Журнал действий: {len(rows)} записей за год")]
    lines += [
        f"{ctx.local(r.at)} {escape(r.actor_fio or str(r.actor_id))}: {escape(r.action)}"
        for r in reversed(rows[-10:])
    ]
    stamp = ctx.now.astimezone(ctx.config.zone).strftime("%Y-%m-%d")
    ctx.reply("\n".join(lines), [Attachment(f"Журнал действий {stamp}.csv", data)])


# ── Отправка сообщения ────────────────────────────────────────────────────────


def message_text(body: str) -> str:
    """Текст после «Сообщение:»: продолжение строки и все строки ниже."""
    lines = body.split("\n")
    for index, line in enumerate(lines):
        key, colon, rest = line.partition(":")
        if colon and text.same_name(key, MESSAGE_KEY):
            return "\n".join([rest.strip(), *lines[index + 1 :]]).strip()
    return ""


async def _send(ctx: Ctx, command: Command, role: Role, files: list[int], message_id: int) -> None:
    if not command.argument:
        refused(
            ctx,
            "Отправить сообщение: укажите получателей "
            "(Пример: Отправить сообщение: Иванов Иван Иванович, 123456)",
        )
        return
    body = message_text(command.body)
    if not body:
        refused(ctx, "Сообщение: не указан текст. Напишите его со второй строки: Сообщение: текст")
        return
    if text.fold(command.argument) in EVERYONE_WORDS:
        everyone = [m for m in await members(ctx) if m.has_access or m.is_admin]
        people = [Person(m.user_id, m.fio) for m in everyone if m.user_id != ctx.sender_id]
        people += [
            Person(owner, "")
            for owner in ctx.config.owner_ids
            if owner != ctx.sender_id and owner not in {p.user_id for p in people}
        ]
        if not people:
            refused(ctx, "Отправить сообщение: получателей нет")
            return
    else:
        chosen = await _people(ctx, command, command.argument)
        if chosen is None:
            return
        people = chosen
    await pending.put(
        ctx,
        SEND,
        {
            "people": [as_dict(p) for p in people],
            "body": body,
            "message_id": message_id,
            "file_ids": files,
        },
    )
    shown = people[:30]
    names = _list(shown) + (f"\nи еще {len(people) - len(shown)}" if len(people) > 30 else "")
    attached = f"\nФайлов: {len(files)}" if files else ""
    ctx.reply(
        bold(f"Отправить сообщение: получателей {len(people)}")
        + "\n"
        + names
        + "\n"
        + bold("Сообщение:")
        + "\n"
        + escape(body)
        + attached
        + "\n\n"
        + markup.PROMPT
    )


async def _deliver_message(
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
    elif kind == "delete_many":
        refused(
            ctx,
            f"Ожидается подтверждение: Удаление, документов: {len(payload['cards'])}. "
            "Подтвердите: Да или Нет. Для нового запроса сначала ответьте Нет",
        )
    elif kind == GRANT:
        refused(ctx, "Ожидается ответ: Да, Без инструкции или Нет")
    elif kind == copies.BACKUP_KIND:
        refused(
            ctx,
            "Ожидается ответ о резервной копии: Да, номер другой папки или Нет. "
            "Для нового запроса сначала ответьте Нет",
        )
    elif kind == copies.REPLACE_KIND:
        refused(
            ctx,
            "Ожидается подтверждение замены журнала: Да или Нет. "
            "Для нового запроса сначала ответьте Нет",
        )
    else:
        refused_headed(ctx, "Ожидается подтверждение. Для нового запроса сначала ответьте Нет")


Handler = Callable[[Ctx, Command, Role, list[int], int], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {
    HELP: _help,
    INSTRUCTION: _instruction,
    ADMIN_INSTRUCTION: _admin_instruction,
    OWNER_INSTRUCTION: _owner_instruction,
    BACKUP: _backup,
    REPLACE: _replace,
    GRANT: _people_command,
    REVOKE: _people_command,
    SEND_INSTRUCTION: _people_command,
    ADMIN: _people_command,
    UNADMIN: _people_command,
    USERS: _users,
    EXPORT: _export,
    AUDIT: _audit,
    SEND: _send,
    ADMINS: _admins,
}
