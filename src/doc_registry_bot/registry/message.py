"""Разбор и проверка сообщения о регистрации в том виде, в каком его набирает человек.

    Действие: Регистрация
    Номер: АБВГ.301121.ХХХ
    Наименование: Кронштейн
    Проект: Машина

Обработчик выбирает ФИО по правам отправителя: у пользователя из карточки мессенджера,
у администратора допускается строка «ФИО» другого сотрудника. Выбранное ФИО передается
в sender_fio и проверяется вместе с остальными полями.

Все ошибки — сразу, по строке на поле: ответ «не указан проект», за которым после исправления
приходит «неправильный номер», — это три круга переписки вместо одного.

Номер пишут по маске: «ХХХ» — следующий свободный номер серии, «001-ХХ» — следующее свободное
исполнение документа 001. Похожая латиница переводится в русские буквы до всякой проверки.
"""

from dataclasses import dataclass, field

from doc_registry_bot.registry import designation, eskd, schema, text
from doc_registry_bot.registry.names import is_full_name

ACTION_KEY = "Действие"
NUMBER_KEY = "Номер"
NAME_KEY = "Наименование"
PROJECT_KEY = "Проект"
FIO_KEY = "ФИО"

REGISTER_WORD = "Регистрация"
NUMBER_EXAMPLE = "АААА.000000.ХХХ или АААА.000000.001-ХХ"
FIO_EXAMPLE = "Иванов Иван Иванович"

NUMBER_MARK = "ХХХ"
EXECUTION_MARK = "ХХ"

# Предельное число номеров в одном сообщении, чтобы ответ оставался читаемым.
MAX_BATCH = 100

ORG_LENGTH = 4


@dataclass(frozen=True)
class RegistrationRequest:
    """Заявка на регистрацию, проверенная целиком.

    `number` — номер документа, исполнение которого заводится; None — новый документ.
    """

    org: str
    eskd: str
    number: int | None
    name: str
    project: str
    fio: str
    accounting: str = ""
    first_use: str = ""
    summary: str = ""

    @property
    def new_execution(self) -> bool:
        return self.number is not None


@dataclass(frozen=True)
class BatchItem:
    """Один номер списка: место в заявке, исходный текст, заявка или ее ошибки."""

    position: int
    raw: str
    request: RegistrationRequest | None
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BatchRead:
    """Разбор списка. Записывать можно, только если `ok`: одна ошибка отклоняет всю заявку."""

    items: list[BatchItem]
    problems: list[str]
    total: int | None = None

    @property
    def count(self) -> int:
        return self.total if self.total is not None else len(self.items)

    @property
    def ok(self) -> bool:
        return (
            not self.problems
            and bool(self.items)
            and all(item.request is not None and not item.problems for item in self.items)
        )


def parse_number(value: str | None) -> tuple[str, str, int | None] | None:
    """Номер по маске: «АБВГ.301121.ХХХ» — новый документ, «АБВГ.301121.001-ХХ» — исполнение.

    Пробелы внутри номера ничего не значат, кроме спешки: «АБВГ. 301121. ХХХ».
    """
    compact = "".join(c for c in (value or "") if not c.isspace())
    parts = compact.split(".")
    if len(parts) != 3:
        return None
    org = text.org(parts[0])
    if len(org) != ORG_LENGTH or not all(text.is_russian_capital(c) for c in org):
        return None
    if not eskd.is_full_code(parts[1]):
        return None
    tail = text.org(parts[2])
    if tail == NUMBER_MARK:
        return org, parts[1], None
    if tail.find("-") != 3 or len(tail) != 4 + len(EXECUTION_MARK):
        return None
    if not all(c in "0123456789" for c in tail[:3]) or tail[4:] != EXECUTION_MARK:
        return None
    number = int(tail[:3])
    if not designation.MIN_NUMBER <= number <= designation.MAX_NUMBER:
        return None
    return org, parts[1], number


def exact_number(value: str) -> str | None:
    """Полный номер документа или исполнения для просмотра, удаления и изменения; иначе None."""
    parts = "".join(c for c in value if not c.isspace()).split(".")
    if len(parts) != 3:
        return None
    org = text.org(parts[0])
    if len(org) != ORG_LENGTH or not all(text.is_russian_capital(c) for c in org):
        return None
    if not eskd.is_full_code(parts[1]):
        return None
    tail = parts[2].split("-")
    if not 1 <= len(tail) <= 2 or len(tail[0]) != 3 or not tail[0].isascii():
        return None
    if not tail[0].isdigit():
        return None
    number = int(tail[0])
    if not designation.MIN_NUMBER <= number <= designation.MAX_NUMBER:
        return None
    if len(tail) == 1:
        return designation.format_number(org, parts[1], number)
    execution_text = tail[1]
    if len(execution_text) != 2 or not execution_text.isascii() or not execution_text.isdigit():
        return None
    execution = int(execution_text)
    if not designation.MIN_EXECUTION <= execution <= designation.MAX_EXECUTION:
        return None
    return designation.format_execution(org, parts[1], number, execution)


def listed(org: str, org_codes: list[str]) -> str | None:
    """Код организации в написании из настроек; список пуст — сам код; None — кода в списке нет."""
    if not org_codes:
        return org
    for code in org_codes:
        if text.org(code) == org:
            return code
    return None


def _common_problems(values: list[tuple[str, str]], sender_fio: str | None) -> list[str]:
    problems: list[str] = []
    action = text.value(values, ACTION_KEY)
    if not action:
        problems.append(f"{ACTION_KEY}: не указано действие (Пример: {REGISTER_WORD})")
    elif not text.same_name(action, REGISTER_WORD):
        problems.append(f"{ACTION_KEY}: неизвестное действие (Пример: {REGISTER_WORD})")
    return problems


def _fio_problems(fio: str) -> list[str]:
    if not fio:
        return [f"{FIO_KEY}: не указано ФИО (Пример: {FIO_EXAMPLE})"]
    if not is_full_name(fio):
        return [f"{FIO_KEY}: не полное ФИО (Пример: {FIO_EXAMPLE})"]
    return []


def read(
    message: str | None, org_codes: list[str], sender_fio: str | None = None
) -> tuple[RegistrationRequest | None, list[str]]:
    """Разобрать сообщение: заявка либо перечень ошибок по строке на поле."""
    return read_values(text.lines(message), org_codes, sender_fio)


def read_values(
    values: list[tuple[str, str]], org_codes: list[str], sender_fio: str | None = None
) -> tuple[RegistrationRequest | None, list[str]]:
    """То же по готовым парам «ключ, значение»: так читаются и строки книги Excel."""
    problems = _common_problems(values, sender_fio)

    number_text = text.value(values, NUMBER_KEY)
    parsed = parse_number(number_text) if number_text else None
    if not number_text:
        problems.append(f"{NUMBER_KEY}: не указан номер (Пример: {NUMBER_EXAMPLE})")
    elif parsed is None:
        problems.append(f"{NUMBER_KEY}: неправильный номер (Пример: {NUMBER_EXAMPLE})")
    elif listed(parsed[0], org_codes) is None:
        problems.append(
            f"{NUMBER_KEY}: неизвестный код организации {parsed[0]} (Можно: {', '.join(org_codes)})"
        )

    name = text.value(values, NAME_KEY)
    if not name:
        problems.append(f"{NAME_KEY}: не указано наименование")
    project = text.value(values, PROJECT_KEY)
    if not project:
        problems.append(f"{PROJECT_KEY}: не указан проект")
    fio = text.collapse(sender_fio if sender_fio is not None else text.value(values, FIO_KEY))
    problems.extend(_fio_problems(fio))

    if problems or parsed is None:
        return None, problems
    org = listed(parsed[0], org_codes) or parsed[0]
    return RegistrationRequest(
        org,
        parsed[1],
        parsed[2],
        name,
        project,
        fio,
        accounting=text.value(values, schema.ACCOUNTING),
        first_use=text.value(values, schema.FIRST_USE),
        summary=text.value(values, schema.SUMMARY),
    ), []


def read_batch(
    message: str | None, org_codes: list[str], sender_fio: str | None = None
) -> BatchRead:
    """Номера через запятую в строке «Номер», остальные поля общие для всего списка.

    Порядок, повторы и пустые элементы сохраняются: одинаковые маски означают несколько
    документов, а случайная двойная запятая — ошибку, не сокращение заявки.
    """
    values = text.lines(message)
    common = _common_problems(values, sender_fio)
    name = text.value(values, NAME_KEY)
    if not name:
        common.append(f"{NAME_KEY}: не указано наименование")
    project = text.value(values, PROJECT_KEY)
    if not project:
        common.append(f"{PROJECT_KEY}: не указан проект")
    fio = text.collapse(sender_fio if sender_fio is not None else text.value(values, FIO_KEY))
    common.extend(_fio_problems(fio))

    number_text = text.value(values, NUMBER_KEY)
    numbers = number_text.split(",", MAX_BATCH)
    if len(numbers) > MAX_BATCH:
        common.append(f"{NUMBER_KEY}: в одной заявке допускается не больше {MAX_BATCH} номеров")
        return BatchRead([], common, number_text.count(",") + 1)

    items: list[BatchItem] = []
    for index, part in enumerate(numbers):
        raw = part.strip()
        problems: list[str] = []
        parsed = parse_number(raw) if raw else None
        if not raw:
            problems.append(f"{NUMBER_KEY}: не указан номер (Пример: {NUMBER_EXAMPLE})")
        elif parsed is None:
            pieces = "".join(c for c in raw if not c.isspace()).split(".")
            detail = (
                "; код классификации в середине должен содержать ровно шесть цифр"
                if len(pieces) == 3 and not eskd.is_full_code(pieces[1])
                else ""
            )
            problems.append(f"{NUMBER_KEY}: неправильный номер{detail} (Пример: {NUMBER_EXAMPLE})")
        elif listed(parsed[0], org_codes) is None:
            problems.append(
                f"{NUMBER_KEY}: неизвестный код организации {parsed[0]} "
                f"(Можно: {', '.join(org_codes)})"
            )
        request = None
        if not problems and parsed is not None:
            org = listed(parsed[0], org_codes) or parsed[0]
            request = RegistrationRequest(
                org,
                parsed[1],
                parsed[2],
                name,
                project,
                fio,
                accounting=text.value(values, schema.ACCOUNTING),
                first_use=text.value(values, schema.FIRST_USE),
                summary=text.value(values, schema.SUMMARY),
            )
        items.append(BatchItem(index + 1, raw, request, problems))
    return BatchRead(items, common)


def known_lines(message: str | None) -> int:
    """Сколько строк сообщения узнано: ноль на непустом тексте — прислали не заявку."""
    values = text.lines(message)
    return sum(
        1
        for key in (
            ACTION_KEY,
            NUMBER_KEY,
            NAME_KEY,
            PROJECT_KEY,
            FIO_KEY,
            schema.ACCOUNTING,
            schema.FIRST_USE,
            schema.SUMMARY,
        )
        if any(text.same_name(name, key) for name, _ in values)
    )
