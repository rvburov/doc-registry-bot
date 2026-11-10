"""Столбцы журнала КД: восемь, в этом порядке, как в журнале SmartBase.

Те же имена ищутся в шапке книги Excel: по именам, в любом порядке, лишние пропускаются.
"""

from collections.abc import Callable, Sequence

from doc_registry_bot.registry.text import same_name

TITLE = "Наименование"
DESIGNATION = "Обозначение"
FIO = "ФИО регистрирующего"
DATE = "Дата регистрации"
PROJECT = "Проект"
ACCOUNTING = "Отметка учета"
FIRST_USE = "Примечание (первичное применение)"
SUMMARY = "Примечание (краткая характеристика)"

COLUMNS: tuple[str, ...] = (TITLE, DESIGNATION, FIO, DATE, PROJECT, ACCOUNTING, FIRST_USE, SUMMARY)


def find[T](columns: Sequence[T], name: Callable[[T], str]) -> tuple[list[T] | None, str]:
    """Найти столбцы журнала по именам — в порядке журнала.

    None — какого-то столбца нет или он встречается дважды; причина во втором значении.
    """
    found: list[T] = []
    missing: list[str] = []
    for wanted in COLUMNS:
        same = [column for column in columns if same_name(name(column), wanted)]
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
