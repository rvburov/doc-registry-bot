"""Оформление ответов. Примеры строк перенесены из ReplyMarkupTests SmartBase."""

import re

import pytest

from doc_registry_bot.registry import instructions, markup
from tests.texts import assert_human_text


@pytest.mark.parametrize(
    ("plain", "html"),
    [
        (
            "Номер: неправильный номер (Пример: АААА.000000.ХХХ или АААА.000000.001-ХХ)",
            "<b>Номер:</b> неправильный номер "
            "(Пример: <i>АААА.000000.ХХХ или АААА.000000.001-ХХ</i>)",
        ),
        (
            "Номер: неизвестный код организации ЖЗИК (Можно: АБВГ, ГМБВ)",
            "<b>Номер:</b> неизвестный код организации ЖЗИК (Можно: <i>АБВГ, ГМБВ</i>)",
        ),
        (
            "Номер: документ АБВГ.301121.005 уже зарегистрирован",
            "<b>Номер:</b> документ <b>АБВГ.301121.005</b> уже зарегистрирован",
        ),
        (
            "Номер: нужен полный номер документа или исполнения "
            "(Пример: АБВГ.301121.001 или АБВГ.301121.001-01)",
            "<b>Номер:</b> нужен полный номер документа или исполнения "
            "(Пример: <i>АБВГ.301121.001 или АБВГ.301121.001-01</i>)",
        ),
        (
            "Ожидается подтверждение: Удаление, АБВГ.301121.001-01. Подтвердите: Да или Нет. "
            "Для нового запроса сначала ответьте Нет",
            "<b>Ожидается подтверждение:</b> Удаление, <b>АБВГ.301121.001-01</b>. "
            "Подтвердите: <b>Да</b> или <b>Нет</b>. Для нового запроса сначала ответьте <b>Нет</b>",
        ),
        (
            "Цвет<1> & вес: изменение этого поля не поддерживается",
            "<b>Цвет&lt;1&gt; &amp; вес:</b> изменение этого поля не поддерживается",
        ),
        (
            "Примечание (первичное применение): изменение этого поля не поддерживается",
            "<b>Примечание (первичное применение):</b> изменение этого поля не поддерживается",
        ),
        (
            "Прим. ред: изменение этого поля не поддерживается",
            "Прим. ред: изменение этого поля не поддерживается",
        ),
    ],
)
def test_a_problem_line_has_its_field_bold_and_its_example_italic(plain: str, html: str) -> None:
    assert markup.problem(plain) == html
    assert markup.plain(html) == plain


@pytest.mark.parametrize(
    ("plain", "html"),
    [
        (
            "Серия изменилась после запроса подтверждения. Регистрация не выполнена. "
            "Отправьте запрос заново",
            "<b>Серия изменилась после запроса подтверждения.</b> Регистрация не выполнена. "
            "Отправьте запрос заново",
        ),
        (
            "Ожидается подтверждение. Для нового запроса сначала ответьте Нет",
            "<b>Ожидается подтверждение.</b> Для нового запроса сначала ответьте <b>Нет</b>",
        ),
    ],
)
def test_a_headed_refusal_has_its_first_sentence_bold(plain: str, html: str) -> None:
    assert markup.headed(plain) == html
    assert markup.plain(html) == plain


def test_escaping_covers_all_three_signs_and_reads_back() -> None:
    assert markup.escape("a <b> &amp; c") == "a &lt;b&gt; &amp;amp; c"
    assert markup.plain(markup.escape("a <b> &amp; c")) == "a <b> &amp; c"


def test_blocks_hold_no_line_feeds() -> None:
    text = instructions.TEXT
    blocks = re.findall(r"<(blockquote|ul)>.*?</\1>", text, flags=re.DOTALL)
    assert len(blocks) > 15
    for block in re.finditer(r"<(blockquote|ul)>.*?</\1>", text, flags=re.DOTALL):
        assert "\n" not in block.group(0)
    assert "\n<blockquote>" not in text
    assert "\n<ul>" not in text


def test_the_instruction_has_its_heading_sections_and_answers_marked() -> None:
    text = instructions.TEXT
    assert text.startswith("<b><font size='4'>Инструкция по работе с журналом КД</font></b>\n")
    for section in (
        "1. Регистрация нового документа",
        "2. Просмотр документа",
        "3. Удаление документа",
        "4. Изменение документа",
        "Правила подтверждения",
        "Если возникла ошибка",
    ):
        assert f"<b>{section}</b>" in text
    assert "<b>Да</b>" in text
    assert "<b>Нет</b>" in text


def test_the_instruction_is_written_like_a_person() -> None:
    assert_human_text(markup.plain(instructions.GREETING))
