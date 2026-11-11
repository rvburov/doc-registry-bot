"""Люди в командах: по ID или поиском по ФИО среди контактов бота, роли."""

from dataclasses import asdict, dataclass

from sqlalchemy import select

from doc_registry_bot.bot.context import Ctx
from doc_registry_bot.bot.types import UserCard
from doc_registry_bot.db.models import Member
from doc_registry_bot.registry import text
from doc_registry_bot.registry.names import is_full_name


@dataclass(frozen=True)
class Person:
    user_id: int
    fio: str
    detail: str = ""

    def label(self) -> str:
        name = self.fio or "ФИО не заполнено"
        extra = f" ({self.detail})" if self.detail else ""
        return f"{name}, ID {self.user_id}{extra}"


@dataclass(frozen=True)
class Role:
    owner: bool
    admin: bool
    access: bool

    @property
    def can_register(self) -> bool:
        return self.owner or self.admin or self.access

    @property
    def is_admin(self) -> bool:
        return self.owner or self.admin


async def role_of(ctx: Ctx, user_id: int) -> Role:
    member = await ctx.session.get(Member, user_id)
    return Role(
        owner=user_id in ctx.config.owner_ids,
        admin=bool(member and member.is_admin),
        access=bool(member and member.has_access),
    )


def person_of(card: UserCard) -> Person:
    detail = ", ".join(part for part in (card.division, card.post) if part)
    return Person(card.user_id, text.collapse(card.fio), detail)


def as_dict(person: Person) -> dict[str, object]:
    return asdict(person)


def from_dict(data: dict[str, object]) -> Person:
    return Person(int(str(data["user_id"])), str(data["fio"]), str(data.get("detail", "")))


@dataclass
class Resolution:
    """Итог поиска людей по списку из команды: найденные и ошибки по строке на человека."""

    found: list[Person]
    problems: list[str]


def tokens_of(argument: str) -> list[str]:
    """Список из команды: через запятую или точку с запятой."""
    return [part.strip() for part in argument.replace(";", ",").split(",") if part.strip()]


async def resolve(ctx: Ctx, tokens: list[str]) -> Resolution:
    """Найти каждого: число — ID, иначе ТОЛЬКО полное ФИО, совпадающее целиком.

    Части ФИО не ищутся: «Иванов» легко попадает не в того человека. Два человека с одним
    полным ФИО — бот не выбирает сам и просит ID.
    """
    result = Resolution([], [])
    contacts: list[Person] | None = None
    for token in tokens:
        if token.isdigit():
            card = await ctx.messenger.user_card(int(token))
            if card is None:
                result.problems.append(f"ID {token}: не найден в мессенджере")
            else:
                result.found.append(person_of(card))
            continue
        fio = text.collapse(token)
        if not is_full_name(fio):
            result.problems.append(f"{token}: укажите полное ФИО или ID")
            continue
        if contacts is None:
            contacts = [person_of(card) for card in await ctx.messenger.contacts()]
        same = [p for p in contacts if text.fold(p.fio) == text.fold(fio)]
        if not same:
            result.problems.append(f"{fio}: не найден среди контактов бота, укажите ID")
        elif len(same) == 1:
            result.found.append(same[0])
        else:
            listed = "; ".join(
                f"ID {p.user_id}" + (f" ({p.detail})" if p.detail else "") for p in same
            )
            result.problems.append(f"{fio}: найдено несколько человек, укажите ID: {listed}")
    return result


def unique(people: list[Person]) -> list[Person]:
    seen: set[int] = set()
    result: list[Person] = []
    for person in people:
        if person.user_id not in seen:
            seen.add(person.user_id)
            result.append(person)
    return result


async def members(ctx: Ctx) -> list[Member]:
    rows = await ctx.session.scalars(select(Member).order_by(Member.fio, Member.user_id))
    return list(rows)


async def remember(ctx: Ctx, person: Person) -> Member:
    """Строка человека в базе; ФИО обновляется по карточке."""
    member = await ctx.session.get(Member, person.user_id, with_for_update=True)
    if member is None:
        member = Member(user_id=person.user_id, fio=person.fio, has_access=False, is_admin=False)
        ctx.session.add(member)
    elif person.fio:
        member.fio = person.fio
    member.updated_at = ctx.now
    await ctx.session.flush()
    return member
