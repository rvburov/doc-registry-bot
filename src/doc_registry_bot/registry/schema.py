"""Столбцы журнала КД: десять, в этом порядке.

Те же имена ищутся в шапке книги Excel: по именам, в любом порядке, лишние пропускаются.
"""

from collections.abc import Callable, Sequence

from doc_registry_bot.registry.text import same_name

TITLE = "Наименование"
DESIGNATION = "Обозначение"
FIO = "ФИО регистрирующего"
DATE = "Дата регистрации"
PROJECT = "Проект"
DIVISION = "Подразделение регистрирующего"
STATUS = "Актуальность"
REGISTERED = "Зарегистрирован"
CANCELED = "Аннулирован"
STATUSES = (REGISTERED, CANCELED)
ACCOUNTING = "Отметка учета"
FIRST_USE = "Примечание первичное"
SUMMARY = "Примечание краткая характеристика"

COLUMNS: tuple[str, ...] = (
    TITLE,
    DESIGNATION,
    PROJECT,
    DATE,
    FIO,
    DIVISION,
    STATUS,
    ACCOUNTING,
    FIRST_USE,
    SUMMARY,
)
LEGACY_COLUMNS = (
    TITLE,
    DESIGNATION,
    FIO,
    DATE,
    PROJECT,
    ACCOUNTING,
    "Примечание (первичное применение)",
    "Примечание (краткая характеристика)",
)


def field_name(value: str) -> str:
    """Прежние названия примечаний остаются допустимыми в сообщениях и файлах."""
    for old, new in zip(LEGACY_COLUMNS[-2:], (FIRST_USE, SUMMARY), strict=True):
        if same_name(value, old):
            return new
    return value


def status(value: str) -> str | None:
    """Допустимое значение актуальности в едином написании."""
    return next((item for item in STATUSES if same_name(item, value)), None)


def find[T](columns: Sequence[T], name: Callable[[T], str]) -> tuple[list[T] | None, str]:
    """Найти столбцы журнала по именам — в порядке журнала.

    None — какого-то столбца нет или он встречается дважды; причина во втором значении.
    """
    found: list[T] = []
    missing: list[str] = []
    for wanted in COLUMNS:
        same = [column for column in columns if same_name(field_name(name(column)), wanted)]
        if len(same) > 1:
            return None, f"столбец «{wanted}» встречается несколько раз, непонятно, какой читать"
        if not same:
            missing.append(wanted)
        else:
            found.append(same[0])
    if len(missing) == 1:
        return None, f"нет столбца «{missing[0]}»"
    if missing:
        return None, "нет столбцов: " + ", ".join(f"«{column}»" for column in missing)
    return found, ""
