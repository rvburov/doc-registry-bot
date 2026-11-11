"""Обозначения по ГОСТ 2.201-80."""

import pytest

from doc_registry_bot.registry import designation as d


def test_number_is_padded_to_three_digits() -> None:
    assert d.format_number("АБВГ", "301121", 1) == "АБВГ.301121.001"
    assert d.format_number("АБВГ", "301121", 42) == "АБВГ.301121.042"
    assert d.format_number("АБВГ", "301121", 999) == "АБВГ.301121.999"


def test_prefix_ends_with_a_dot() -> None:
    assert d.prefix("АБВГ", "301121") == "АБВГ.301121."


def test_number_is_read_from_its_own_series() -> None:
    assert d.number_in("АБВГ.301121.007", "АБВГ", "301121") == 7


def test_other_organisation_does_not_occupy_a_number() -> None:
    assert d.number_in("ГМБВ.301121.007", "АБВГ", "301121") is None


def test_other_eskd_code_does_not_occupy_a_number() -> None:
    assert d.number_in("АБВГ.723611.007", "АБВГ", "301121") is None


def test_case_of_the_organisation_code_is_ignored() -> None:
    assert d.number_in("абвг.301121.007", "АБВГ", "301121") == 7


def test_surrounding_spaces_are_ignored() -> None:
    assert d.number_in("  АБВГ.301121.007  ", "АБВГ", "301121") == 7


@pytest.mark.parametrize("value", ["АБВГ.301121.001-01", "АБВГ.301121.001 (копия)"])
def test_execution_and_notes_still_occupy_the_base_number(value: str) -> None:
    assert d.number_in(value, "АБВГ", "301121") == 1


@pytest.mark.parametrize("value", ["АБВГ.301121.0011", "АБВГ.301121.01", "АБВГ.301121.1"])
def test_number_must_be_exactly_three_digits(value: str) -> None:
    assert d.number_in(value, "АБВГ", "301121") is None


@pytest.mark.parametrize("value", ["АБВГ.301121.000", "АБВГ.301121.", "АБВГ.301121.abc", "", None])
def test_junk_does_not_occupy_a_number(value: str | None) -> None:
    assert d.number_in(value, "АБВГ", "301121") is None


def test_empty_collection_starts_from_one() -> None:
    assert d.first_free([], "АБВГ", "301121") == 1


def test_gap_is_filled_before_the_tail() -> None:
    assert d.first_free(["АБВГ.301121.001", "АБВГ.301121.003"], "АБВГ", "301121") == 2


def test_numbers_run_on_after_a_solid_range() -> None:
    taken = ["АБВГ.301121.001", "АБВГ.301121.002", "АБВГ.301121.003"]
    assert d.first_free(taken, "АБВГ", "301121") == 4


def test_each_series_counts_on_its_own() -> None:
    taken = ["АБВГ.301121.001", "АБВГ.301121.002", "ГМБВ.723611.001"]
    assert d.first_free(taken, "АБВГ", "723611") == 1
    assert d.first_free(taken, "АБВГ", "301121") == 3


def test_exhausted_series_has_no_free_number() -> None:
    taken = [d.format_number("АБВГ", "301121", n) for n in range(d.MIN_NUMBER, d.MAX_NUMBER + 1)]
    assert d.first_free(taken, "АБВГ", "301121") is None


def test_empty_values_are_skipped() -> None:
    assert d.first_free(["", None, "АБВГ.301121.001"], "АБВГ", "301121") == 2


def test_execution_is_formatted_with_two_digits() -> None:
    assert d.format_execution("АБВГ", "301121", 2, 4) == "АБВГ.301121.002-04"


@pytest.mark.parametrize(
    ("value", "number", "execution"),
    [
        ("АБВГ.301121.002-04", 2, 4),
        ("АБВГ.301121.002-04СБ", 2, 4),
        ("АБВГ.301121.002", 2, None),
        ("АБВГ.301121.002СБ", 2, None),
    ],
)
def test_series_parse_reads_number_and_execution(
    value: str, number: int, execution: int | None
) -> None:
    assert d.parse_in_series(value, "АБВГ", "301121") == (number, execution)


@pytest.mark.parametrize(
    "value", ["АБВГ.301121.002-4", "АБВГ.301121.002-004", "АБВГ.301121.002-00"]
)
def test_sloppy_execution_is_not_read(value: str) -> None:
    assert d.parse_in_series(value, "АБВГ", "301121") == (2, None)


def test_executions_of_a_number_are_listed_in_order() -> None:
    journal = [
        "АБВГ.301121.002",
        "АБВГ.301121.002-03",
        "АБВГ.301121.002-01",
        "АБВГ.301121.003-05",
        "ГМБВ.301121.002-09",
    ]
    assert d.executions_of(journal, "АБВГ", "301121", 2) == [1, 3]


def test_first_free_execution_fills_the_gap() -> None:
    journal = ["АБВГ.301121.002-01", "АБВГ.301121.002-03"]
    assert d.first_free_execution(journal, "АБВГ", "301121", 2) == 2


def test_first_execution_of_a_document_is_01() -> None:
    assert d.first_free_execution(["АБВГ.301121.002"], "АБВГ", "301121", 2) == 1


def test_exhausted_executions_have_no_free_number() -> None:
    journal = [
        d.format_execution("АБВГ", "301121", 2, e)
        for e in range(d.MIN_EXECUTION, d.MAX_EXECUTION + 1)
    ]
    assert d.first_free_execution(journal, "АБВГ", "301121", 2) is None


def test_execution_takes_the_whole_number_too() -> None:
    assert d.first_free(["АБВГ.301121.001", "АБВГ.301121.002-01"], "АБВГ", "301121") == 3


def test_issued_numbers_come_sorted_with_their_executions() -> None:
    journal = [
        "АБВГ.301121.014",
        "АБВГ.301121.002-03",
        "АБВГ.301121.001",
        "АБВГ.301121.002-01",
        "АБВГ.723611.005",
    ]
    issued = d.issued(journal, "АБВГ", "301121")
    assert [item.number for item in issued] == [1, 2, 14]
    assert issued[0].executions == []
    assert issued[1].executions == [1, 3]
    assert issued[2].executions == []


def test_execution_alone_puts_the_number_into_the_list() -> None:
    [single] = d.issued(["АБВГ.301121.002-01"], "АБВГ", "301121")
    assert single.number == 2
    assert single.executions == [1]


def test_empty_series_has_no_issued_numbers() -> None:
    assert d.issued(["АБВГ.723611.005"], "АБВГ", "301121") == []
