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


def test_the_database_is_built_from_postgres_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key in ("MESSENGER_URL", "OWNER_IDS", "ORG_CODES"):
        monkeypatch.setenv(key, GOOD[key])
    monkeypatch.setenv("POSTGRES_DB", "kd")
    monkeypatch.setenv("POSTGRES_USER", "bot")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd%1")
    result = Settings(_env_file=None)  # type: ignore[call-arg]
    assert result.dsn == "postgresql+psycopg://bot:p%40ss%3Aw%2Frd%251@postgres:5432/kd"


def test_a_missing_database_password_stops_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    for key in ("MESSENGER_URL", "OWNER_IDS", "ORG_CODES"):
        monkeypatch.setenv(key, GOOD[key])
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ── Папки копий и напоминание ─────────────────────────────────────────────────


def test_backup_folders_and_the_bot_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    result = settings(
        monkeypatch,
        BOT_DIR="/mnt/e/doc-registry-bot/deploy",
        BACKUP_DIR_2="/mnt/e/Копии",
        BACKUP_DIR_2_NAME="Архив",
        BACKUP_SHARE="//fileserver/копии",
    )
    labels = [folder.label() for folder in result.folders]
    assert labels == [
        "Основная (E:\\doc-registry-bot\\deploy\\backups)",
        "Архив (E:\\Копии)",
        "Сетевая папка (//fileserver/копии)",
    ]


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("E:\\Копии", "без двоеточия"),
        ("копии", "должен начинаться с / или ./"),
        ("/", "весь диск или системная папка"),
        ("/mnt/e", "весь диск или системная папка"),
        ("/etc", "весь диск или системная папка"),
    ],
)
def test_a_bad_backup_folder_stops_the_bot(
    monkeypatch: pytest.MonkeyPatch, value: str, problem: str
) -> None:
    with pytest.raises(ValidationError) as error:
        settings(monkeypatch, BACKUP_DIR_2=value)
    assert problem in str(error.value)


def test_the_reminder_schedule_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    result = settings(
        monkeypatch,
        BACKUP_REMINDER_DAY="ср",
        BACKUP_REMINDER_TIME="09:30",
        BACKUP_REMINDER_ZONE="Asia/Yekaterinburg",
    )
    assert result.reminder is not None
    assert result.reminder.describe() == "среда, 09:30 (Asia/Yekaterinburg)"
    assert settings(monkeypatch, BACKUP_REMINDER_DAY="нет").reminder is None
    for key, value in (("BACKUP_REMINDER_DAY", "пнд"), ("BACKUP_REMINDER_ZONE", "Луна/Море")):
        with pytest.raises(ValidationError):
            settings(monkeypatch, **{key: value})
