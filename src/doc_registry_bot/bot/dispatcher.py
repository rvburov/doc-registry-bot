"""Обработка одного входящего сообщения: от защиты от повторов до готовых ответов.

Все изменения и отметка «сообщение обработано» — одна транзакция: повторная доставка или
дочитывание истории не выполнит сообщение второй раз. Ответы уходят после коммита.
"""

import logging
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot import commands, documents, pending, registration
from doc_registry_bot.bot.context import BotConfig, Ctx
from doc_registry_bot.bot.people import Role, person_of, remember, role_of
from doc_registry_bot.bot.registration import refused, refused_headed
from doc_registry_bot.bot.types import Incoming, Messenger, MessengerError, Reply
from doc_registry_bot.db.models import Member, Pending, ProcessedMessage
from doc_registry_bot.registry import markup, message, text
from doc_registry_bot.registry.names import is_full_name

log = logging.getLogger(__name__)

FAILED = "Не удалось обработать сообщение"
WITHOUT_INSTRUCTION = "без инструкции"
# ФИО для первой проверки текста: карточку из мессенджера незачем читать, пока сообщение
# с ошибками. Настоящее ФИО подставляется перед регистрацией.
_ANY_FULL_NAME = "Проверка Полного Имени"


class Dispatcher:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], config: BotConfig, messenger: Messenger
    ) -> None:
        self._sessions = sessions
        self._config = config
        self._messenger = messenger

    async def handle(self, incoming: Incoming, now: datetime) -> list[Reply]:
        """Обработать сообщение; пустой список — оно уже было обработано раньше."""
        try:
            async with self._sessions() as session, session.begin():
                fresh = await session.scalar(
                    pg_insert(ProcessedMessage)
                    .values(
                        message_id=incoming.message_id,
                        sender_id=incoming.sender_id,
                        processed_at=now,
                    )
                    .on_conflict_do_nothing()
                    .returning(ProcessedMessage.message_id)
                )
                if fresh is None:
                    return []
                ctx = Ctx(session, self._config, self._messenger, incoming.sender_id, now)
                await route(ctx, incoming)
                return ctx.replies
        except Exception:
            log.exception("Сообщение %s не обработано", incoming.message_id)
            await self._mark_failed(incoming, now)
            return [Reply(incoming.sender_id, FAILED)]

    async def handle_stale(
        self, sender_id: int, message_ids: list[int], now: datetime
    ) -> list[Reply]:
        """Сообщения старше окна дочитывания: не выполняются, человек получает просьбу
        отправить их заново. Отмечаются обработанными, чтобы просьба не повторялась."""
        async with self._sessions() as session, session.begin():
            fresh = 0
            for message_id in message_ids:
                inserted = await session.scalar(
                    pg_insert(ProcessedMessage)
                    .values(message_id=message_id, sender_id=sender_id, processed_at=now)
                    .on_conflict_do_nothing()
                    .returning(ProcessedMessage.message_id)
                )
                fresh += inserted is not None
            if not fresh:
                return []
            ctx = Ctx(session, self._config, self._messenger, sender_id, now)
            role = await role_of(ctx, sender_id)
            if not role.can_register:
                return []
            if fresh == 1:
                note = "Сообщение пришло, пока бот не работал. Отправьте его заново."
            else:
                note = f"Сообщения ({fresh}) пришли, пока бот не работал. Отправьте их заново."
            return [Reply(sender_id, markup.bold(note))]

    async def _mark_failed(self, incoming: Incoming, now: datetime) -> None:
        """Упавшее сообщение тоже помечается обработанным: иначе дочитывание истории повторяло
        бы его при каждом запуске."""
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(
                    pg_insert(ProcessedMessage)
                    .values(
                        message_id=incoming.message_id,
                        sender_id=incoming.sender_id,
                        processed_at=now,
                    )
                    .on_conflict_do_nothing()
                )
        except Exception:
            log.exception("Не удалось отметить сообщение %s", incoming.message_id)


async def route(ctx: Ctx, incoming: Incoming) -> None:
    role = await role_of(ctx, ctx.sender_id)
    member = await ctx.session.get(Member, ctx.sender_id)
    # ФИО для журнала действий; для регистрации его все равно уточняет карточка мессенджера.
    ctx.sender_fio = member.fio if member else ""
    if not role.can_register:
        ctx.reply(markup.problems(commands.DENIED))
        return
    plain = text.plain(incoming.text).strip()
    answer = text.confirmation(incoming.text)
    command = commands.parse(plain)
    values = text.lines(incoming.text)
    action = text.value(values, message.ACTION_KEY)
    known = documents.known_action(action)

    found = await pending.current(ctx)
    if isinstance(found, Pending):
        if await _answer_pending(ctx, role, found, plain, answer):
            return
        # Пока ждем ответа, доступны только справка и просмотр.
        if command is not None and command.word in commands.ALWAYS:
            await commands.run(ctx, command, role, incoming.file_ids, incoming.message_id)
        elif command is None and known == documents.VIEW and not incoming.file_ids:
            await documents.run(ctx, known, values)
        else:
            commands.pending_refusal(ctx, found.kind, found.payload)
        return
    if answer is not None:
        if found == pending.EXPIRED:
            refused(ctx, "Подтверждение: срок истек. Отправьте запрос заново")
        else:
            refused(ctx, "Подтверждение: нет операции, ожидающей ответа. Отправьте запрос заново")
        return
    if command is not None:
        await commands.run(ctx, command, role, incoming.file_ids, incoming.message_id)
        return
    if incoming.file_ids:
        await _register_file(ctx, incoming)
        return
    if action and not text.same_name(action, message.REGISTER_WORD) and known is None:
        refused(ctx, f"Действие: неизвестное действие (Можно: {documents.CHOICES})")
        return
    if known is not None:
        await documents.run(ctx, known, values)
        return
    await _register_text(ctx, incoming.text)


async def _answer_pending(
    ctx: Ctx, role: Role, found: Pending, plain: str, answer: bool | None
) -> bool:
    """Ответ на ожидание. False — сообщение ответом не является."""
    kind, payload = found.kind, dict(found.payload)
    if kind in commands.PEOPLE_COMMANDS:
        without = kind == commands.GRANT and text.fold(plain) == WITHOUT_INSTRUCTION
        if answer is None and not without:
            return False
        await pending.take(ctx, found)
        if answer is not False and not commands.allowed(kind, role):
            refused(ctx, commands.NO_RIGHTS)
            return True
        reply = "нет" if answer is False else "без инструкции" if without else "да"
        await commands.confirm(ctx, kind, payload, reply)
        return True
    if answer is None:
        return False
    await pending.take(ctx, found)
    if answer is False:
        ctx.reply(markup.bold("Операция отменена"))
        return True
    if kind in ("register", "batch", "file"):
        await registration.confirm(ctx, kind, payload)
    elif kind in ("delete", "edit"):
        await documents.confirm(ctx, kind, payload)
    return True


async def _sender_fio(ctx: Ctx) -> str | None:
    """ФИО отправителя из карточки мессенджера; None — ответ уже дан."""
    try:
        card = await ctx.messenger.user_card(ctx.sender_id)
    except MessengerError:
        refused(ctx, "Мессенджер: не удалось получить вашу карточку. Повторите запрос позже")
        return None
    fio = text.collapse(card.fio) if card else ""
    if card is None or not is_full_name(fio):
        refused(
            ctx,
            "ФИО: в вашей карточке мессенджера нет полного ФИО. Заполните фамилию, имя и "
            "отчество и отправьте запрос заново",
        )
        return None
    await remember(ctx, person_of(card))
    ctx.sender_fio = fio
    return fio


async def _register_text(ctx: Ctx, body: str) -> None:
    values = text.lines(body)
    if "," in text.value(values, message.NUMBER_KEY):
        fio = await _sender_fio(ctx)
        if fio is None:
            return
        batch = message.read_batch(body, ctx.orgs, fio)
        await registration.register_batch(ctx, batch)
        return
    request, problems = message.read(body, ctx.orgs, sender_fio=_ANY_FULL_NAME)
    if request is None:
        refused(ctx, "\n".join(problems))
        return
    fio = await _sender_fio(ctx)
    if fio is None:
        return
    request, problems = message.read(body, ctx.orgs, fio)
    if request is None:
        refused(ctx, "\n".join(problems))
        return
    await registration.register(ctx, request)


async def _register_file(ctx: Ctx, incoming: Incoming) -> None:
    if len(incoming.file_ids) > 1:
        refused(ctx, "Файл: отправьте одну книгу в сообщении")
        return
    fio = await _sender_fio(ctx)
    if fio is None:
        return
    try:
        attachment = await ctx.messenger.download(incoming.message_id, incoming.file_ids[0])
    except MessengerError:
        refused_headed(ctx, "Файл не получен. Отправьте книгу заново")
        return
    await registration.register_file(ctx, attachment.data, attachment.name or "Вложение.xlsx")
