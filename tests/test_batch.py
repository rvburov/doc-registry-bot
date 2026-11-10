"""Список номеров через запятую. Примеры перенесены из BatchMessageParsingTests SmartBase."""

import pytest

from doc_registry_bot.registry import message as m

ORGS = ["АБВГ", "ГМБВ"]


def message(
    numbers: str,
    name: str = "Кронштейн",
    project: str = "Машина",
    fio: str = "Иванов Иван Иванович",
    action: str = "Регистрация",
) -> str:
    return (
        f"Действие: {action}\nНомер: {numbers}\nНаименование: {name}\nПроект: {project}\nФИО: {fio}"
    )


def test_mixed_series_and_repeated_masks_keep_order_and_common_fields() -> None:
    parsed = m.read_batch(message("АБВГ.301121.ХХХ, ГМБВ.806121.ХХХ, АБВГ.301121.ХХХ"), ORGS)
    assert parsed.ok
    assert parsed.problems == []
    assert [item.position for item in parsed.items] == [1, 2, 3]
    requests = [item.request for item in parsed.items]
    assert all(request is not None for request in requests)
    assert [r.eskd for r in requests if r] == ["301121", "806121", "301121"]
    assert requests[1] is not None and requests[1].org == "ГМБВ"
    assert requests[0] == requests[2]
    for item in parsed.items:
        assert item.problems == []
        assert item.request is not None
        assert item.request.name == "Кронштейн"
        assert item.request.project == "Машина"
        assert item.request.fio == "Иванов Иван Иванович"
        assert item.request.number is None


def test_every_invalid_number_is_reported_without_losing_valid_items() -> None:
    parsed = m.read_batch(
        message("АБВГ.30112.ХХХ, АБВГ.301121.ХХХ, АБВГ.301121.ХХХ-ХХ, АБВГ.301121.000-ХХ"), ORGS
    )
    assert not parsed.ok
    assert parsed.problems == []
    assert len(parsed.items) == 4
    assert parsed.items[0].request is None
    assert parsed.items[1].request is not None
    assert parsed.items[2].request is None
    assert parsed.items[3].request is None
    for item in parsed.items:
        if item.position != 2:
            assert len(item.problems) == 1
    assert "шесть цифр" in parsed.items[0].problems[0]


@pytest.mark.parametrize("code", ["30112", "3011210", "30112О", "３０１１２１"])
def test_middle_code_requires_exactly_six_ascii_digits(code: str) -> None:
    parsed = m.read_batch(message(f"АБВГ.{code}.ХХХ, АБВГ.301121.ХХХ"), ORGS)
    assert not parsed.ok
    [problem] = parsed.items[0].problems
    assert "шесть цифр" in problem
    assert parsed.items[1].request is not None


def test_catalog_absence_is_not_a_syntax_error() -> None:
    parsed = m.read_batch(message("АБВГ.301127.ХХХ, АБВГ.806121.ХХХ, АБВГ.000000.ХХХ"), ORGS)
    assert parsed.ok
    assert all(item.problems == [] for item in parsed.items)


@pytest.mark.parametrize(
    ("numbers", "count", "invalid"),
    [
        ("", 1, 1),
        (",АБВГ.301121.ХХХ", 2, 1),
        ("АБВГ.301121.ХХХ,", 2, 2),
        ("АБВГ.301121.ХХХ, ,АБВГ.301121.ХХХ", 3, 2),
    ],
)
def test_empty_items_are_errors_in_their_original_positions(
    numbers: str, count: int, invalid: int
) -> None:
    parsed = m.read_batch(message(numbers), ORGS)
    assert not parsed.ok
    assert len(parsed.items) == count
    empty = parsed.items[invalid - 1]
    assert empty.position == invalid
    assert empty.raw == ""
    assert empty.request is None
    [problem] = empty.problems
    assert problem.startswith("Номер: не указан номер")


def test_common_errors_appear_once_and_all_number_errors_are_retained() -> None:
    parsed = m.read_batch(
        message(
            "ошибка, АБВГ.301121.ХХХ, неверный",
            name="",
            project="",
            fio="Иванов И.И.",
            action="Удаление",
        ),
        ORGS,
    )
    assert not parsed.ok
    assert len(parsed.problems) == 4
    for key in ("Действие:", "Наименование:", "Проект:", "ФИО:"):
        assert any(problem.startswith(key) for problem in parsed.problems)
    assert len(parsed.items[0].problems) == 1
    assert parsed.items[1].problems == []
    assert parsed.items[1].request is not None
    assert len(parsed.items[2].problems) == 1


def test_unknown_organizations_are_checked_separately() -> None:
    parsed = m.read_batch(message("ЖЗИК.301121.ХХХ, АБВГ.301121.ХХХ, ПРСТ.806121.ХХХ"), ORGS)
    assert not parsed.ok
    assert "ЖЗИК" in parsed.items[0].problems[0]
    assert parsed.items[1].problems == []
    assert "ПРСТ" in parsed.items[2].problems[0]
    for item in parsed.items:
        if item.problems:
            assert "АБВГ, ГМБВ" in item.problems[0]


def test_number_and_execution_masks_remain_available_together() -> None:
    parsed = m.read_batch(message("АБВГ.301121.ХХХ, АБВГ.301121.014-XX, АБВГ.806121.XXX"), ORGS)
    assert parsed.ok
    first, second, third = (item.request for item in parsed.items)
    assert first is not None and not first.new_execution
    assert second is not None and second.new_execution and second.number == 14
    assert third is not None and third.number is None


@pytest.mark.parametrize("number", ["АБВГ.301121.123", "АБВГ.301121.123-01"])
def test_a_full_number_is_an_error_without_losing_the_other_masks(number: str) -> None:
    parsed = m.read_batch(message(f"АБВГ.301121.ХХХ, {number}"), ORGS)
    assert not parsed.ok
    assert parsed.problems == []
    assert parsed.items[0].request is not None
    assert parsed.items[0].problems == []
    assert parsed.items[1].request is None
    [problem] = parsed.items[1].problems
    assert problem.startswith("Номер: неправильный номер")


def test_markup_spaces_and_case_follow_single_registration() -> None:
    body = message("абвг. 301121. xxx , АБВГ.806121.ХХХ", fio=" Иванов   Иван  Иванович ")
    parsed = m.read_batch("<blockquote>" + body.replace("\n", "<br>") + "</blockquote>", ORGS)
    assert parsed.ok
    for item in parsed.items:
        assert item.request is not None
        assert item.request.org == "АБВГ"
        assert item.request.fio == "Иванов Иван Иванович"
    assert parsed.items[0].raw == "абвг. 301121. xxx"


def test_commas_in_common_fields_do_not_split_the_list() -> None:
    parsed = m.read_batch(
        message("АБВГ.301121.ХХХ, АБВГ.806121.ХХХ", name="Вал, опора", project="Машина, узел"),
        ORGS,
    )
    assert parsed.ok
    assert len(parsed.items) == 2
    for item in parsed.items:
        assert item.request is not None
        assert item.request.name == "Вал, опора"
        assert item.request.project == "Машина, узел"


def test_duplicate_field_uses_the_first_value_like_single_registration() -> None:
    parsed = m.read_batch(
        message("АБВГ.301121.ХХХ, АБВГ.806121.ХХХ") + "\nНомер: ошибка\nПроект: Другой", ORGS
    )
    assert parsed.ok
    assert len(parsed.items) == 2
    assert all(item.request and item.request.project == "Машина" for item in parsed.items)


def test_batch_limit_is_inclusive_and_excess_is_a_whole_request_error() -> None:
    permitted = ", ".join(["АБВГ.301121.ХХХ"] * m.MAX_BATCH)
    parsed = m.read_batch(message(permitted), ORGS)
    excessive = m.read_batch(message(permitted + ", АБВГ.301121.ХХХ"), ORGS)
    assert parsed.ok
    assert len(parsed.items) == m.MAX_BATCH
    assert not excessive.ok
    assert excessive.items == []
    [problem] = excessive.problems
    assert str(m.MAX_BATCH) in problem
    assert excessive.count == m.MAX_BATCH + 1


def test_missing_everything_reports_common_fields_and_missing_number() -> None:
    parsed = m.read_batch("", ORGS)
    assert not parsed.ok
    assert len(parsed.problems) == 4
    [item] = parsed.items
    assert item.request is None
    [problem] = item.problems
    assert problem.startswith("Номер: не указан номер")
