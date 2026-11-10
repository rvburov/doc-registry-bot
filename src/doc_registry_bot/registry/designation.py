"""Обозначение документа по ГОСТ 2.201-80: «код организации.код ЕСКД.порядковый номер».

Например «АБВГ.301121.001». Номер ищется В ПРЕДЕЛАХ СЕРИИ, то есть пары «код организации + код
ЕСКД»: у каждого классификатора своя нумерация 001...999. Поэтому все разборы получают серию
отдельно, а не угадывают ее делением строки по точкам.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

MIN_NUMBER = 1
MAX_NUMBER = 999
MIN_EXECUTION = 1
MAX_EXECUTION = 99


@dataclass(frozen=True)
class IssuedNumber:
    """Выданный номер серии и его занятые исполнения (пусто — исполнений нет)."""

    number: int
    executions: list[int] = field(default_factory=list)


def prefix(org: str, eskd: str) -> str:
    """Начало обозначений серии: «АБВГ.301121.» (с точкой на конце)."""
    return f"{org}.{eskd}."


def format_number(org: str, eskd: str, number: int) -> str:
    """Готовое обозначение: номер всегда тремя цифрами («2» → «002»)."""
    return f"{prefix(org, eskd)}{number:03d}"


def format_execution(org: str, eskd: str, number: int, execution: int) -> str:
    """Обозначение исполнения: «АБВГ.301121.002-04». Исполнение всегда двумя цифрами."""
    return f"{format_number(org, eskd, number)}-{execution:02d}"


def _digits(text: str) -> int:
    count = 0
    while count < len(text) and text[count] in "0123456789":
        count += 1
    return count


def parse_in_series(designation: str | None, org: str, eskd: str) -> tuple[int, int | None] | None:
    """Номер и исполнение обозначения серии; None — не из этой серии или номер не по ГОСТ.

    Цифр номера должно быть РОВНО ТРИ: «01» с равным правом читается как 001, 011 или 014, и
    гадать за человека нельзя. Исполнение читается только в каноне «-NN»; иначе строка остается
    занятым номером без исполнения, как и «...002СБ».
    """
    text = (designation or "").strip()
    head = prefix(org, eskd)
    if text[: len(head)].upper() != head.upper():
        return None
    tail = text[len(head) :]
    digits = _digits(tail)
    if digits != 3:
        return None
    number = int(tail[:3])
    if not MIN_NUMBER <= number <= MAX_NUMBER:
        return None
    return number, _execution_after(tail[3:])


def _execution_after(rest: str) -> int | None:
    if len(rest) < 3 or rest[0] != "-":
        return None
    if _digits(rest[1:]) != 2:
        return None
    execution = int(rest[1:3])
    return execution if MIN_EXECUTION <= execution <= MAX_EXECUTION else None


def number_in(designation: str | None, org: str, eskd: str) -> int | None:
    """Номер обозначения серии; None — чужая серия номер не занимает.

    Исполнение («...001-01») и приписка («...001 (копия)») занимают номер 001: это тот же документ.
    """
    parsed = parse_in_series(designation, org, eskd)
    return parsed[0] if parsed else None


def taken_numbers(designations: Iterable[str | None], org: str, eskd: str) -> set[int]:
    """Занятые номера серии набором."""
    taken: set[int] = set()
    for designation in designations:
        number = number_in(designation, org, eskd)
        if number is not None:
            taken.add(number)
    return taken


def first_free_among(taken: set[int]) -> int | None:
    """Первый свободный номер; дырки заполняются. None — заняты все 001...999."""
    for number in range(MIN_NUMBER, MAX_NUMBER + 1):
        if number not in taken:
            return number
    return None


def first_free(designations: Iterable[str | None], org: str, eskd: str) -> int | None:
    """Первый свободный номер серии среди уже выданных обозначений."""
    return first_free_among(taken_numbers(designations, org, eskd))


def taken_executions(
    designations: Iterable[str | None], org: str, eskd: str, number: int
) -> set[int]:
    """Занятые исполнения одного номера серии набором."""
    taken: set[int] = set()
    for designation in designations:
        parsed = parse_in_series(designation, org, eskd)
        if parsed and parsed[0] == number and parsed[1] is not None:
            taken.add(parsed[1])
    return taken


def executions_of(
    designations: Iterable[str | None], org: str, eskd: str, number: int
) -> list[int]:
    """Занятые исполнения одного номера по возрастанию."""
    return sorted(taken_executions(designations, org, eskd, number))


def first_free_execution_among(taken: set[int]) -> int | None:
    """Первое свободное исполнение; None — заняты все 01...99."""
    for execution in range(MIN_EXECUTION, MAX_EXECUTION + 1):
        if execution not in taken:
            return execution
    return None


def first_free_execution(
    designations: Iterable[str | None], org: str, eskd: str, number: int
) -> int | None:
    """Первое свободное исполнение номера."""
    return first_free_execution_among(taken_executions(designations, org, eskd, number))


def issued(designations: Iterable[str | None], org: str, eskd: str) -> list[IssuedNumber]:
    """Все выданные номера серии по возрастанию, у каждого — его занятые исполнения."""
    found: dict[int, set[int]] = {}
    for designation in designations:
        parsed = parse_in_series(designation, org, eskd)
        if parsed is None:
            continue
        executions = found.setdefault(parsed[0], set())
        if parsed[1] is not None:
            executions.add(parsed[1])
    return [IssuedNumber(number, sorted(found[number])) for number in sorted(found)]
