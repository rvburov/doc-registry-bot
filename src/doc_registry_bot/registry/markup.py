"""Оформление ответов разметкой HTML: программа-мессенджер показывает текст сообщения как HTML.

Что та сторона показывает (проверено пользователем в SmartBase): жирный, курсив, зачеркнутый,
размер шрифта, цитата, список. Не показывает: цвет, заголовки, абзацы <p>, код и перевод строки.
Поэтому строки ответа разделяются «\\n», а при отправке он заменяется на <br>. Внутри цитаты и
списка «\\n» не ставится: замененный на перенос, он дал бы лишние пустые строки.

Значения журнала и сообщения экранируются: наименование «Кронштейн <левый>» иначе пропало бы
как тег.
"""

import re

from doc_registry_bot.registry import text

ANSWER_PROMPT = "Подтвердите: <b>Да</b> или <b>Нет</b>"
LIFETIME = "<i>Подтверждение действует 10 минут</i>"
PROMPT = ANSWER_PROMPT + "\n" + LIFETIME


def escape(value: str | None) -> str:
    """Знаки, которые разметка поняла бы по-своему, — сущностями."""
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bold(markup: str) -> str:
    return f"<b>{markup}</b>"


def italic(markup: str) -> str:
    return f"<i>{markup}</i>"


def struck(markup: str) -> str:
    """Зачеркнутое старое значение; пустому зачеркивать нечего."""
    return f"<s>{markup}</s>" if markup else ""


def larger(markup: str) -> str:
    """Крупнее обычного: заголовки h1-h6 та сторона не увеличивает."""
    return f"<font size='4'>{markup}</font>"


def quote(*lines: str) -> str:
    """Образец сообщения цитатой, строки внутри — тегом переноса."""
    return "<blockquote>" + "<br>".join(lines) + "</blockquote>"


def bullets(*items: str) -> str:
    """Пояснения и правила списком."""
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


# Пример и перечень допустимого в скобках — курсивом, номер документа вне их — жирным.
_MARKED = re.compile(
    r"(?P<hint>\((?:Пример|Можно): )(?P<text>[^()]*)\)"
    r"|(?P<number>[А-ЯA-Z]{4}\.[0-9]{6}\.[0-9]{3}(?:-[0-9]{2})?)"
)

# Название поля с двоеточием в начале строки ошибки. Точка до двоеточия — уже предложение.
_FIELD = re.compile(r"^[^:.<>]{1,60}:(?=\s|$)")


def _mark(match: re.Match[str]) -> str:
    if match.group("number"):
        return bold(match.group(0))
    return f"{match.group('hint')}{italic(match.group('text'))})"


def problem(line: str) -> str:
    """Строка ошибки: поле жирным, пример курсивом, номер документа жирным. Слова не меняются."""
    markup = _MARKED.sub(_mark, escape(line))
    markup = _FIELD.sub(lambda m: bold(m.group(0)), markup, count=1)
    return markup.replace("Подтвердите: Да или Нет", ANSWER_PROMPT).replace(
        "ответьте Нет", "ответьте <b>Нет</b>"
    )


def problems(lines: str) -> str:
    """Ошибки по строке на каждую."""
    return "\n".join(problem(line) for line in lines.split("\n"))


def headed(line: str) -> str:
    """Отказ, первое предложение которого — заголовок жирным."""
    end = line.find(". ")
    if end < 0:
        return bold(escape(line))
    return bold(escape(line[: end + 1])) + " " + problem(line[end + 2 :])


def plain(markup: str) -> str:
    """Что увидит человек: текст без тегов, сущности знаками."""
    return text.unmarked(markup)
