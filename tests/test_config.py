"""Настройки из .env: неверное значение останавливает бота с понятной причиной."""

import pytest
from pydantic import ValidationError

from doc_registry_bot.config import Settings

GOOD = {
    "MESSENGER_URL": "http://messenger:8888/",
    "OWNER_IDS": "123, 456",
    "ORG_CODES": "АБВГ,ГМБВ",
    "DATABASE_URL": "postgresql+psycopg://bot:secret@postgres/bot",
}


def settings(monkeypatch: pytest.MonkeyPatch, **changes: str) -> Settings:
    for key, value in {**GOOD, **changes}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_good_values_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    result = settings(monkeypatch)
    assert result.messenger_url == "http://messenger:8888"
    assert result.owner_ids == [123, 456]
    assert result.org_codes == ["АБВГ", "ГМБВ"]
    assert result.zone.key == "Europe/Moscow"
    assert result.catchup_hours == 24


@pytest.mark.parametrize(
    ("key", "value", "words"),
    [
        ("ORG_CODES", "ABBG", "4 заглавные русские буквы"),
        ("ORG_CODES", "АБВ", "4 заглавные русские буквы"),
        ("ORG_CODES", "АБВГ,АБВГ", "дважды"),
        ("ORG_CODES", "", "хотя бы один"),
        ("OWNER_IDS", "Иванов", "не числовой"),
        ("OWNER_IDS", "", "хотя бы один"),
        ("MESSENGER_URL", "messenger:8888", "http://"),
        ("TIME_ZONE", "Марс/Олимп", "часовой пояс"),
    ],
)
def test_a_wrong_value_names_the_reason(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str, words: str
) -> None:
    with pytest.raises(ValidationError, match=words):
        settings(monkeypatch, **{key: value})
