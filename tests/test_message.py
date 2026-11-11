"""Разбор сообщения о регистрации: примеры «сообщение → ожидаемый результат».

Коды организации задаются в .env и проверяются при запуске, поэтому написание кода проверяется
по списку из настроек.
"""

import pytest

from doc_registry_bot.registry import message as m
from doc_registry_bot.registry import text

ORGS = ["АБВГ", "ГМБВ"]

GOOD = (
    "Действие: Регистрация\nНомер: АБВГ.301121.ХХХ\nНаименование: Кронштейн\n"
    "Проект: Машина\nФИО: Иванов Иван Иванович"
)


def message(
    number: str = "АБВГ.301121.ХХХ",
    name: str = "Кронштейн",
    project: str = "Машина",
    fio: str = "Иванов Иван Иванович",
    action: str = "Регистрация",
) -> str:
    return (
        f"Действие: {action}\nНомер: {number}\nНаименование: {name}\nПроект: {project}\nФИО: {fio}"
    )


def test_a_good_message_becomes_a_request() -> None:
    request, problems = m.read(GOOD, ORGS)
    assert problems == []
    assert request is not None
    assert request.org == "АБВГ"
    assert request.eskd == "301121"
    assert request.number is None
    assert not request.new_execution
    assert request.name == "Кронштейн"
    assert request.project == "Машина"
    assert request.fio == "Иванов Иван Иванович"


def test_the_sender_card_fio_wins_over_the_text() -> None:
    request, problems = m.read(message(fio="Кто-то Другой"), ORGS, "Петров Петр Петрович")
    assert problems == []
    assert request is not None
    assert request.fio == "Петров Петр Петрович"


@pytest.mark.parametrize("line_break", ["\r\n", "\r", " ", " "])
def test_any_line_break_separates_the_lines(line_break: str) -> None:
    request, problems = m.read(message().replace("\n", line_break), ORGS)
    assert problems == []
    assert request is not None
    assert request.project == "Машина"
    assert request.fio == "Иванов Иван Иванович"


@pytest.mark.parametrize(
    ("opening", "line_break", "closing"),
    [
        ("<span>", "<br>", "</span>"),
        ('<span style="color: black">', "<br/>", "</span>"),
        ("", "<br />", ""),
        ("", "<BR>", ""),
        ("<p>", "</p><p>", "</p>"),
        ("<div>", "</div><div>", "</div>"),
        ("<div><b>", "</b><br>\n<b>", "</b></div>"),
    ],
)
def test_markup_is_read_as_the_lines_a_person_sees(
    opening: str, line_break: str, closing: str
) -> None:
    request, problems = m.read(opening + message().replace("\n", line_break) + closing, ORGS)
    assert problems == []
    assert request is not None
    assert request.org == "АБВГ"
    assert request.number is None
    assert request.name == "Кронштейн"
    assert request.project == "Машина"
    assert request.fio == "Иванов Иван Иванович"


def test_entities_of_a_markup_become_characters() -> None:
    body = message(name="Вал &amp; втулка &lt;левые&gt;", fio="Иванов&nbsp;Иван Иванович")
    request, problems = m.read("<span>" + body.replace("\n", "<br>") + "</span>", ORGS)
    assert problems == []
    assert request is not None
    assert request.name == "Вал & втулка <левые>"
    assert request.fio == "Иванов Иван Иванович"


@pytest.mark.parametrize("name", ["Вал <левый>", "Вал &amp; втулка", "Вал < 5 мм > 3 мм"])
def test_text_without_tags_is_left_alone(name: str) -> None:
    request, problems = m.read(message(name=name), ORGS)
    assert problems == []
    assert request is not None
    assert request.name == name


def test_an_escaped_tag_stays_text() -> None:
    assert text.plain("<span>строка&lt;br&gt;та же строка</span>") == "строка<br>та же строка"


@pytest.mark.parametrize(
    ("value", "plain"),
    [
        (None, ""),
        ("", ""),
        ("обычный текст", "обычный текст"),
        ("<span>одна<br>две</span>", "одна\nдве"),
    ],
)
def test_plain_text_is_what_a_person_sees(value: str | None, plain: str) -> None:
    assert text.plain(value) == plain


@pytest.mark.parametrize(
    ("value", "known"),
    [
        ("", 0),
        ("{text}", 0),
        ("Добрый день: нужен номер", 0),
        ("Проект: Машина", 1),
        ("проект: Машина\nФИО: Иванов", 2),
        ("<span>Проект: Машина<br>ФИО: Иванов</span>", 2),
    ],
)
def test_known_lines_are_counted(value: str, known: int) -> None:
    assert m.known_lines(value) == known


@pytest.mark.parametrize("number", ["АБВГ.301121.001-ХХ", "АБВГ.301121.001-XX"])
def test_a_numbered_mask_asks_for_an_execution(number: str) -> None:
    request, problems = m.read(message(number), ORGS)
    assert problems == []
    assert request is not None
    assert request.number == 1
    assert request.new_execution


def test_line_order_and_key_case_do_not_matter() -> None:
    request, problems = m.read(
        "фио: Иванов Иван Иванович\nПРОЕКТ: Машина\nнаименование: Кронштейн\n"
        "номер: АБВГ.301121.ХХХ\nдействие: регистрация",
        ORGS,
    )
    assert problems == []
    assert request is not None


@pytest.mark.parametrize("number", ["АБВГ.301121.XXX", "абвг.301121.ххх", "АБВГ. 301121. ХХХ"])
def test_look_alike_letters_and_spaces_are_forgiven(number: str) -> None:
    request, problems = m.read(message(number), ORGS)
    assert problems == []
    assert request is not None
    assert request.org == "АБВГ"


def test_latin_look_alikes_in_the_code_are_read_as_russian() -> None:
    request, problems = m.read(message("ABBГ.301121.ХХХ"), ["АВВГ"])
    assert problems == []
    assert request is not None
    assert request.org == "АВВГ"


def test_all_problems_come_at_once() -> None:
    request, problems = m.read(
        "Действие: Регистрация\nНомер: АБВГ.301121.000\nНаименование: Кронштейн\nФИО: Иванов И.И.",
        ORGS,
    )
    assert request is None
    assert problems == [
        "Номер: неправильный номер (Пример: АААА.000000.ХХХ или АААА.000000.001-ХХ)",
        "Проект: не указан проект",
        "ФИО: не полное ФИО (Пример: Иванов Иван Иванович)",
    ]


def test_an_empty_message_lists_everything_missing() -> None:
    request, problems = m.read("", ORGS)
    assert request is None
    assert len(problems) == 5
    for problem, key in zip(
        problems, ["Действие:", "Номер:", "Наименование:", "Проект:", "ФИО:"], strict=True
    ):
        assert problem.startswith(key)


@pytest.mark.parametrize(
    "number",
    [
        "АБВГ.301121.001",
        "АБВГ.301121.123",
        "АБВГ.301121.999",
        "АБВГ.301121.014-01",
        "АБВГ.301121.ХХХ-ХХ",
        "АБВГ.301121",
        "АБВГ.30112.ХХХ",
        "АБ.301121.ХХХ",
        "АБВГ.301121.000-ХХ",
        "QWER.301121.ХХХ",
    ],
)
def test_a_number_off_the_mask_is_refused(number: str) -> None:
    request, problems = m.read(message(number), ORGS)
    assert request is None
    assert any(p.startswith("Номер: неправильный номер") for p in problems)


def test_an_unknown_org_code_is_named_with_the_allowed_ones() -> None:
    _, problems = m.read(message("ЖЗИК.301121.ХХХ"), ORGS)
    [problem] = problems
    assert "ЖЗИК" in problem
    assert "АБВГ, ГМБВ" in problem


def test_an_eskd_code_outside_the_catalog_is_deferred_to_confirmation() -> None:
    request, problems = m.read(message("АБВГ.000000.ХХХ"), ORGS)
    assert problems == []
    assert request is not None
    assert request.eskd == "000000"


def test_an_unknown_action_is_refused() -> None:
    _, problems = m.read(message(action="Удаление"), ORGS)
    [problem] = problems
    assert problem.startswith("Действие: неизвестное действие")


def test_extra_spaces_in_the_name_are_folded() -> None:
    request, problems = m.read(message(fio="  Иванов   Иван  Иванович "), ORGS)
    assert problems == []
    assert request is not None
    assert request.fio == "Иванов Иван Иванович"


def test_the_first_mention_of_a_key_wins() -> None:
    request, _ = m.read(GOOD + "\nПроект: Другой", ORGS)
    assert request is not None
    assert request.project == "Машина"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("АБВГ.301121.001", "АБВГ.301121.001"),
        ("абвг. 301121.001-01", "АБВГ.301121.001-01"),
        ("ABBГ.301121.001", "АВВГ.301121.001"),
        ("АБВГ.301121.ХХХ", None),
        ("АБВГ.301121.000", None),
        ("АБВГ.301121.001-00", None),
        ("АБВГ.301121.001-1", None),
        ("АБВГ.30112.001", None),
    ],
)
def test_an_exact_number_is_full_or_nothing(value: str, expected: str | None) -> None:
    assert m.exact_number(value) == expected
