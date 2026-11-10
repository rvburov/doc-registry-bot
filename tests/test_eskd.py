"""Справочник ЕСКД. Примеры перенесены из EskdCatalogTests SmartBase."""

import pytest

from doc_registry_bot.registry import eskd


def test_catalog_has_all_five_levels() -> None:
    entries = eskd.entries()
    assert len(entries) == 78_481
    by_level: dict[int, int] = {}
    for entry in entries:
        by_level[len(entry.code)] = by_level.get(len(entry.code), 0) + 1
    assert by_level == {2: 43, 3: 290, 4: 1_722, 5: 10_240, 6: 66_186}


def test_codes_are_ordered_as_a_tree() -> None:
    codes = [entry.code for entry in eskd.entries()]
    assert codes == sorted(codes)


def test_catalog_names_use_one_spelling() -> None:
    assert all("ё" not in entry.name.lower() for entry in eskd.entries())


def test_a_code_with_children_occurs_once() -> None:
    seen: set[str] = set()
    twice = set()
    for entry in eskd.entries():
        if len(entry.code) == eskd.CODE_LENGTH:
            continue
        if entry.code in seen:
            twice.add(entry.code)
        seen.add(entry.code)
    assert twice == set()


def test_wrapped_name_is_glued_back() -> None:
    node = eskd.node("4814")
    assert node is not None
    assert node.name.startswith("Опрокидыватели")
    assert node.name.endswith("кантования")


@pytest.mark.parametrize("code", ["30458", "30754", "75045"])
def test_missing_node_is_built(code: str) -> None:
    node = eskd.node(code)
    assert node is not None
    assert node.name == ""


def test_every_code_is_reachable_from_its_class() -> None:
    known = {entry.code for entry in eskd.entries()}
    orphans = [
        (entry.code, entry.code[:length])
        for entry in eskd.entries()
        for length in range(eskd.ROOT_LENGTH, len(entry.code))
        if entry.code[:length] not in known
    ]
    assert orphans == []


def test_duplicated_full_code_keeps_both_rows() -> None:
    names = [entry.name for entry in eskd.entries() if entry.code == "337641"]
    assert names == ["Браслеты", "Броши"]


def test_known_code_is_found() -> None:
    assert eskd.node("040000") is not None
    assert eskd.node("040000").name == "По двум и более видам норм"  # type: ignore[union-attr]
    assert eskd.node("38").name == "Двигатели (кроме электрических)"  # type: ignore[union-attr]


def test_missing_code_is_not_found() -> None:
    assert eskd.node("999999") is None


@pytest.mark.parametrize(
    ("code", "found"), [("301121", True), ("38", False), ("0400", False), ("999999", False)]
)
def test_find_answers_only_for_a_full_code(code: str, found: bool) -> None:
    assert (eskd.find(code) is not None) == found


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("301121", True),
        ("04000", False),
        ("3011211", False),
        ("30112a", False),
        ("", False),
        (None, False),
    ],
)
def test_full_code_is_exactly_six_digits(code: str | None, expected: bool) -> None:
    assert eskd.is_full_code(code) == expected


def test_chain_runs_from_the_class_down() -> None:
    assert [e.code for e in eskd.chain("330008")] == ["33", "330", "3300", "33000", "330008"]


def test_chain_of_an_unknown_code_is_empty() -> None:
    assert eskd.chain("999999") == []
