"""Текст сообщения таким, каким его видит человек: без разметки, по строкам «Ключ: значение».

Программа-мессенджер присылает набранное то строками, то разметкой: перенос строки тегом, текст в
обертке. Человек видит те же строки и об обертке не знает, поэтому разбор снимает ее сам.
"""

import html
import re
import unicodedata

# Тег: слово латиницей в угловых скобках. «<левый>» в наименовании — не тег.
_TAG = re.compile(r"</?[a-z][a-z0-9]*(?:\s[^<>]*)?/?>", re.IGNORECASE)

# Теги, за которыми человек видит новую строку: перенос и границы абзаца.
_BREAK_TAG = re.compile(
    r"<br(?:\s[^<>]*)?/?>|</?(?:p|div|li|tr|h[1-6]|blockquote|pre)(?:\s[^<>]*)?>",
    re.IGNORECASE,
)

# Чем разные программы отделяют строку от строки.
_LINE_BREAKS = re.compile("[\n\r  \u0085\v\f]+")

# Метки цитаты и списка, которые попадают перед строкой при копировании оформленного ответа.
_LINE_MARKS = ">|│•·◦▪-–—*"

# Сколько строк сообщения разбирается: вставленная по ошибке простыня не превращается в сто ключей.
MAX_LINES = 100

# Латинские буквы, неотличимые на экране от русских, и их русские пары (только заглавные).
_LOOK_ALIKE = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


def plain(text: str | None) -> str:
    """Текст без разметки. Разметкой текст считается, только если в нем есть хоть один тег."""
    if not text or not _TAG.search(text):
        return text or ""
    return unmarked(text)


def unmarked(markup: str) -> str:
    """Заведомо размеченный текст: без тегов, сущности — знаками.

    Порядок важен: сначала теги, потом сущности. Наоборот «&lt;br&gt;», набранное буквами,
    стало бы переносом строки, которого человек не ставил.
    """
    lines = _BREAK_TAG.sub("\n", markup)
    bare = _TAG.sub("", lines)
    return html.unescape(bare).replace(" ", " ")


def fold(text: str | None) -> str:
    """Свертка для сравнения имен: регистр, крайние пробелы и написание буквы «е» не важны."""
    return (text or "").strip().lower().replace("ё", "е")


def same_name(actual: str | None, expected: str | None) -> bool:
    """Имена равны с точностью до регистра, крайних пробелов и написания буквы «е»."""
    return fold(actual) == fold(expected)


def _without_format_chars(text: str) -> str:
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def _key(text: str) -> str:
    """Ключ строки без меток цитаты и списка и без невидимых знаков форматирования."""
    return _without_format_chars(text).strip().lstrip(_LINE_MARKS).strip()


def lines(text: str | None) -> list[tuple[str, str]]:
    """Строки «Ключ: значение». Первое упоминание ключа побеждает: во втором обычно цитата."""
    values: list[tuple[str, str]] = []
    for line in [part for part in _LINE_BREAKS.split(plain(text)) if part][:MAX_LINES]:
        colon = line.find(":")
        if colon <= 0:
            continue
        key = _key(line[:colon])
        if not key or any(same_name(existing, key) for existing, _ in values):
            continue
        values.append((key, line[colon + 1 :].strip()))
    return values


def value(values: list[tuple[str, str]], key: str) -> str:
    """Значение строки по ключу; нет строки — пусто."""
    for name, found in values:
        if same_name(name, key):
            return found
    return ""


def confirmation(text: str | None) -> bool | None:
    """Ответ на подтверждение: только целое «да» или «нет», без учета регистра."""
    answer = _without_format_chars(plain(text)).strip().lower()
    if answer == "да":
        return True
    if answer == "нет":
        return False
    return None


def org(text: str | None) -> str:
    """Код организации к одному виду: заглавные, похожая латиница — русскими буквами."""
    return (text or "").strip().upper().translate(_LOOK_ALIKE)


def collapse(text: str) -> str:
    """Слова через один пробел."""
    return " ".join(text.split())


def is_russian_capital(char: str) -> bool:
    return "А" <= char <= "Я"
