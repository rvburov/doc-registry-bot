"""Просмотр, удаление и изменение записи журнала по сообщению. Перенесено из SmartBase.

Данные не меняются до «Да», и подтверждается именно показанный человеку снимок: если запись
за время ожидания изменилась или исчезла, операция не выполняется.
"""

from dataclasses import asdict, dataclass, replace
from typing import Any

from doc_registry_bot.bot import pending
from doc_registry_bot.bot.context import Ctx
from doc_registry_bot.bot.registration import refused, refused_headed
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import JournalEntry
from doc_registry_bot.registry import markup, message, text
from doc_registry_bot.registry.markup import bold, escape, struck

VIEW = "Просмотр"
DELETE = "Удаление"
EDIT = "Изменение"
CHOICES = "Регистрация, Просмотр, Удаление, Изменение"
EDITABLE = ("Наименование", "Проект")
EMPTY = "не указано"


@dataclass(frozen=True)
class Card:
    """Снимок записи журнала для ответа и для сверки при подтверждении."""

    entry_id: int
    number: str
    name: str
    fio: str
    date: str
    project: str
    accounting: str
    first_use: str
    summary: str
    version: int

    def html(self, changed: tuple[str, ...] = ()) -> str:
        def show(value: str) -> str:
            return escape(value) if value else EMPTY

        def mark(value: str, field: str) -> str:
            return bold(show(value)) if field in changed else show(value)

        return "\n".join(
            [
                f"Номер: {bold(show(self.number))}",
                f"Наименование: {mark(self.name, 'Наименование')}",
                f"Проект: {mark(self.project, 'Проект')}",
                f"ФИО: {show(self.fio)}",
                f"Дата регистрации: {show(self.date)}",
                f"Отметка учета: {show(self.accounting)}",
                f"Примечание (первичное применение): {show(self.first_use)}",
                f"Примечание (краткая характеристика): {show(self.summary)}",
            ]
        )

    def value(self, field: str) -> str:
        return self.name if field == "Наименование" else self.project


def card_of(ctx: Ctx, entry: JournalEntry) -> Card:
    return Card(
        entry.id,
        entry.designation,
        entry.name,
        entry.fio,
        ctx.local(entry.registered_at),
        entry.project,
        entry.accounting,
        entry.first_use,
        entry.summary,
        entry.version,
    )


def known_action(action: str) -> str | None:
    for known in (VIEW, DELETE, EDIT):
        if text.same_name(known, action):
            return known
    return None


async def run(ctx: Ctx, action: str, values: list[tuple[str, str]]) -> None:
    """Просмотр, удаление или изменение. Ожидающая операция уже проверена вызывающим."""
    problems: list[str] = []
    number_text = text.value(values, message.NUMBER_KEY)
    number = message.exact_number(number_text) if number_text else None
    if not number_text:
        problems.append("Номер: не указан номер")
    elif number is None:
        problems.append(
            "Номер: нужен полный номер документа или исполнения "
            "(Пример: АБВГ.301121.001 или АБВГ.301121.001-01)"
        )
    changes: dict[str, str] = {}
    if action == EDIT:
        for field, value in values:
            if text.same_name(field, message.ACTION_KEY) or text.same_name(
                field, message.NUMBER_KEY
            ):
                continue
            if text.same_name(field, message.FIO_KEY):
                problems.append(
                    "ФИО: регистрирующий определяется по карточке отправителя и не изменяется "
                    "командой."
                )
                continue
            editable = next((f for f in EDITABLE if text.same_name(f, field)), None)
            if editable is None:
                problems.append(f"{field}: изменение этого поля не поддерживается")
            elif not value:
                problems.append(f"{editable}: не указано значение")
            else:
                changes[editable] = value
        if not changes and not problems:
            problems.append("Изменение: укажите хотя бы одно поле, Наименование или Проект")
    else:
        for field, _ in values:
            if not text.same_name(field, message.ACTION_KEY) and not text.same_name(
                field, message.NUMBER_KEY
            ):
                problems.append(f"{field}: для действия «{action}» нужны только Действие и Номер")
    if problems:
        refused(ctx, "\n".join(problems))
        return
    assert number is not None
    entry = await journal.find(ctx.session, number)
    if entry is None:
        refused(ctx, f"Номер: документ {number} не найден")
        return
    card = card_of(ctx, entry)
    if action == VIEW:
        ctx.reply(card.html())
        return
    after = replace(
        card,
        name=changes.get("Наименование", card.name),
        project=changes.get("Проект", card.project),
    )
    if action == EDIT and after == card:
        refused(ctx, "Изменение: новые значения совпадают с текущими. Изменений нет")
        return
    await pending.put(
        ctx, "delete" if action == DELETE else "edit", {"card": asdict(card), "changes": changes}
    )
    if action == DELETE:
        preview = bold("Подтвердите удаление документа:") + "\n" + card.html()
    else:
        lines = "\n".join(
            f"{field}: {struck(escape(card.value(field)))} → {bold(escape(value))}"
            for field, value in changes.items()
        )
        preview = (
            bold("Подтвердите изменение документа:")
            + "\n"
            + card.html()
            + "\n\n"
            + bold("Изменения:")
            + "\n"
            + lines
        )
    ctx.reply(f"{preview}\n\n{markup.PROMPT}")


async def confirm(ctx: Ctx, kind: str, payload: dict[str, Any]) -> None:
    """Ответ «Да» на удаление или изменение."""
    before = Card(**payload["card"])
    changes: dict[str, str] = payload["changes"]
    entry = await ctx.session.get(JournalEntry, before.entry_id, with_for_update=True)
    if entry is None:
        refused(ctx, f"Номер: документ {before.number} не найден")
        return
    current = card_of(ctx, entry)
    if current != before:
        refused_headed(
            ctx,
            "Документ изменился после запроса подтверждения. Операция не выполнена. "
            "Отправьте запрос заново",
        )
        return
    if kind == "delete":
        await ctx.session.delete(entry)
        await ctx.session.flush()
        ctx.audit("Удаление записи", _plain_card(current))
        ctx.reply(bold("Удалено") + "\n" + current.html())
        return
    details = "; ".join(f"{f}: {current.value(f)} → {v}" for f, v in changes.items())
    if "Наименование" in changes:
        entry.name = changes["Наименование"]
    if "Проект" in changes:
        entry.project = changes["Проект"]
    entry.version += 1
    entry.updated_at = ctx.now
    await ctx.session.flush()
    ctx.audit("Изменение записи", f"{entry.designation}: {details}")
    ctx.reply(bold("Изменено") + "\n" + card_of(ctx, entry).html(tuple(changes)))


def _plain_card(card: Card) -> str:
    return markup.plain(card.html()).replace("\n", "; ")
