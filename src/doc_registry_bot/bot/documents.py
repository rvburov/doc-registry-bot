"""Просмотр, удаление и изменение записи журнала по сообщению.

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
from doc_registry_bot.registry import markup, message, schema, text
from doc_registry_bot.registry.markup import bold, escape, struck
from doc_registry_bot.registry.names import is_full_name

VIEW = "Просмотр"
DELETE = "Удаление"
EDIT = "Изменение"
CHOICES = "Регистрация, Просмотр, Удаление, Изменение"
OPTIONAL = (schema.ACCOUNTING, schema.FIRST_USE, schema.SUMMARY)
EDITABLE = ("Наименование", "Проект", *OPTIONAL)
ADMIN_FIELDS = (message.FIO_KEY, schema.DIVISION, schema.STATUS)
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
    division: str = ""
    status: str = schema.REGISTERED

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
                f"ФИО: {mark(self.fio, 'ФИО')}",
                f"Дата регистрации: {show(self.date)}",
                f"{schema.DIVISION}: {mark(self.division, schema.DIVISION)}",
                f"{schema.STATUS}: {mark(self.status, schema.STATUS)}",
                f"Отметка учета: {mark(self.accounting, schema.ACCOUNTING)}",
                f"{schema.FIRST_USE}: {mark(self.first_use, schema.FIRST_USE)}",
                f"{schema.SUMMARY}: {mark(self.summary, schema.SUMMARY)}",
            ]
        )

    def value(self, field: str) -> str:
        return {
            "Наименование": self.name,
            "Проект": self.project,
            "ФИО": self.fio,
            schema.DIVISION: self.division,
            schema.STATUS: self.status,
            schema.ACCOUNTING: self.accounting,
            schema.FIRST_USE: self.first_use,
            schema.SUMMARY: self.summary,
        }[field]


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
        entry.division,
        entry.status,
    )


def known_action(action: str) -> str | None:
    for known in (VIEW, DELETE, EDIT):
        if text.same_name(known, action):
            return known
    return None


async def run(
    ctx: Ctx, action: str, values: list[tuple[str, str]], *, allow_fio: bool = False
) -> None:
    """Просмотр, удаление или изменение. Ожидающая операция уже проверена вызывающим."""
    values = [(schema.field_name(k), v) for k, v in values]
    problems: list[str] = []
    number_text = text.value(values, message.NUMBER_KEY)
    if "," in number_text:
        if action == DELETE:
            await _delete_many(ctx, number_text, values)
        else:
            refused(ctx, f"Номер: для действия «{action}» указывается один номер")
        return
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
                if not allow_fio:
                    problems.append(
                        "ФИО: регистрирующий определяется по карточке отправителя и не изменяется "
                        "командой."
                    )
                else:
                    fio = text.collapse(value)
                    if not fio:
                        problems.append("ФИО: не указано значение")
                    elif not is_full_name(fio):
                        problems.append(
                            "ФИО: укажите полные фамилию, имя и отчество, без инициалов"
                        )
                    else:
                        changes[message.FIO_KEY] = fio
                continue
            protected = next(
                (f for f in (schema.DIVISION, schema.STATUS) if text.same_name(f, field)), None
            )
            if protected is not None:
                if not allow_fio:
                    problems.append(f"{protected}: изменение доступно только администратору")
                elif protected == schema.STATUS:
                    status = schema.status(value)
                    if status is None:
                        problems.append(
                            f"{protected}: допустимо только {', '.join(schema.STATUSES)}"
                        )
                    else:
                        changes[protected] = status
                else:
                    changes[protected] = text.collapse(value)
                continue
            editable = next((f for f in EDITABLE if text.same_name(f, field)), None)
            if editable is None:
                problems.append(f"{field}: изменение этого поля не поддерживается")
            elif not value and editable not in OPTIONAL:
                problems.append(f"{editable}: не указано значение")
            else:
                changes[editable] = value
        if not changes and not problems:
            fields = (*EDITABLE, *ADMIN_FIELDS) if allow_fio else EDITABLE
            problems.append(f"Изменение: укажите хотя бы одно поле: {', '.join(fields)}")
    else:
        problems.extend(_extra_fields(action, values))
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
        fio=changes.get(message.FIO_KEY, card.fio),
        division=changes.get(schema.DIVISION, card.division),
        status=changes.get(schema.STATUS, card.status),
        accounting=changes.get(schema.ACCOUNTING, card.accounting),
        first_use=changes.get(schema.FIRST_USE, card.first_use),
        summary=changes.get(schema.SUMMARY, card.summary),
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
            f"{field}: {struck(escape(card.value(field) or EMPTY))} → "
            f"{bold(escape(value or EMPTY))}"
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


def _extra_fields(action: str, values: list[tuple[str, str]]) -> list[str]:
    return [
        f"{field}: для действия «{action}» нужны только Действие и Номер"
        for field, _ in values
        if not text.same_name(field, message.ACTION_KEY)
        and not text.same_name(field, message.NUMBER_KEY)
    ]


async def _delete_many(ctx: Ctx, number_text: str, values: list[tuple[str, str]]) -> None:
    """Удаление нескольких документов одним подтверждением. Не нашелся хоть один — ничего."""
    problems = _extra_fields(DELETE, values)
    parts = [part.strip() for part in number_text.split(",")]
    if len(parts) > message.MAX_BATCH:
        problems.append(f"Номер: в одном запросе не больше {message.MAX_BATCH} номеров")
    numbers: list[str] = []
    for raw in parts:
        exact = message.exact_number(raw) if raw else None
        if not raw:
            problems.append("Номер: в списке пустой номер")
        elif exact is None:
            problems.append(f"Номер: {raw}: нужен полный номер документа или исполнения")
        elif exact not in numbers:
            numbers.append(exact)
    if problems:
        refused(ctx, "\n".join(problems))
        return
    cards: list[Card] = []
    missing: list[str] = []
    for number in numbers:
        entry = await journal.find(ctx.session, number)
        if entry is None:
            missing.append(f"Номер: документ {number} не найден")
        else:
            cards.append(card_of(ctx, entry))
    if missing:
        refused(ctx, "\n".join([*missing, "Ничего не удалено"]))
        return
    if len(cards) == 1:
        await pending.put(ctx, "delete", {"card": asdict(cards[0]), "changes": {}})
        preview = bold("Подтвердите удаление документа:") + "\n" + cards[0].html()
        ctx.reply(f"{preview}\n\n{markup.PROMPT}")
        return
    await pending.put(ctx, "delete_many", {"cards": [asdict(card) for card in cards]})
    ctx.reply(
        bold(f"Подтвердите удаление документов: {len(cards)}")
        + "\n"
        + _short_list(cards)
        + "\n\n"
        + markup.PROMPT
    )


def _short_list(cards: list[Card]) -> str:
    return "\n".join(
        f"{index}. {bold(escape(card.number))}: {escape(card.name)}, проект {escape(card.project)}"
        for index, card in enumerate(cards, start=1)
    )


async def _confirm_delete_many(ctx: Ctx, payload: dict[str, Any]) -> None:
    before = [Card(**item) for item in payload["cards"]]
    entries: list[JournalEntry] = []
    for card in before:
        entry = await ctx.session.get(JournalEntry, card.entry_id, with_for_update=True)
        if entry is None or card_of(ctx, entry) != card:
            refused_headed(
                ctx,
                "Документы изменились после запроса подтверждения. Удаление не выполнено. "
                "Отправьте запрос заново",
            )
            return
        entries.append(entry)
    for entry, card in zip(entries, before, strict=True):
        await ctx.session.delete(entry)
        ctx.audit("Удаление записи", _plain_card(card))
    await ctx.session.flush()
    ctx.reply(bold(f"Удалено: {len(before)}") + "\n" + _short_list(before))


async def confirm(ctx: Ctx, kind: str, payload: dict[str, Any]) -> None:
    """Ответ «Да» на удаление или изменение."""
    if kind == "delete_many":
        await _confirm_delete_many(ctx, payload)
        return
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
    details = "; ".join(
        f"{f}: {current.value(f) or EMPTY} → {v or EMPTY}" for f, v in changes.items()
    )
    if "Наименование" in changes:
        entry.name = changes["Наименование"]
    if "Проект" in changes:
        entry.project = changes["Проект"]
    if message.FIO_KEY in changes:
        entry.fio = changes[message.FIO_KEY]
    if schema.DIVISION in changes:
        entry.division = changes[schema.DIVISION]
    if schema.STATUS in changes:
        entry.status = changes[schema.STATUS]
    if schema.ACCOUNTING in changes:
        entry.accounting = changes[schema.ACCOUNTING]
    if schema.FIRST_USE in changes:
        entry.first_use = changes[schema.FIRST_USE]
    if schema.SUMMARY in changes:
        entry.summary = changes[schema.SUMMARY]
    entry.version += 1
    entry.updated_at = ctx.now
    await ctx.session.flush()
    ctx.audit("Изменение записи", f"{entry.designation}: {details}")
    ctx.reply(bold("Изменено") + "\n" + card_of(ctx, entry).html(tuple(changes)))


def _plain_card(card: Card) -> str:
    return markup.plain(card.html()).replace("\n", "; ")
