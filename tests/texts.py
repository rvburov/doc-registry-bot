"""Проверка текстов бота: пишем как сотрудник, а не как ИИ (правило AGENTS.md)."""

import re

FORBIDDEN_CHARS = {
    "—": "длинное тире",
    "–": "короткое тире",
    "ё": "буква е с точками",
    "Ё": "буква Е с точками",
    "…": "многоточие одним знаком",
}

FORBIDDEN_PHRASES = (
    "Отлично!",
    "Конечно!",
    "Готово!",
    "Давайте",
    "Обратите внимание",
    "Надеюсь, это поможет",
    "Если у вас есть вопросы",
    "Я бот",
    "Я - бот",
)

_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️]")


def problems_in(text: str) -> list[str]:
    """Что в тексте выдает ИИ; пусто — текст годится."""
    found = [f"{name}: «{char}»" for char, name in FORBIDDEN_CHARS.items() if char in text]
    found += [f"шаблон «{phrase}»" for phrase in FORBIDDEN_PHRASES if phrase in text]
    if _EMOJI.search(text):
        found.append("эмодзи")
    return found


def assert_human_text(text: str) -> None:
    assert problems_in(text) == [], text
