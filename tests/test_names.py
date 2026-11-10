"""ФИО полностью. Примеры из RegistryImportTests SmartBase."""

import pytest

from doc_registry_bot.registry.names import is_full_name


@pytest.mark.parametrize(
    "fio",
    ["Иванов Иван Иванович", "Римский-Корсаков Николай Андреевич", "  Иванов   Иван   Иванович "],
)
def test_a_full_name_passes(fio: str) -> None:
    assert is_full_name(fio)


@pytest.mark.parametrize(
    "fio",
    [
        "Иванов И.И.",
        "Иванов И. И.",
        "Иванов Иван",
        "Иванов Иван Иванович Младший",
        "Иванов -Иван Иванович",
        "Иванов Иван1 Иванович",
        "Иванов И Иванович",
        "",
        None,
    ],
)
def test_initials_and_junk_do_not_pass(fio: str | None) -> None:
    assert not is_full_name(fio)
