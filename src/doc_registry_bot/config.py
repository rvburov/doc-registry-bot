"""Настройки установки — только из окружения (`.env` на сервере). Ошибка — бот не запускается.

Неверное значение должно остановить бота с понятной причиной в журнале: молча работающий бот с
неправильным кодом организации выдал бы номера не той серии.
"""

from functools import cached_property
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from doc_registry_bot.registry import text


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    messenger_url: str = Field(description="Адрес службы мессенджера: http://контейнер:8888")
    owner_ids: Annotated[list[int], NoDecode] = Field(description="ID владельцев через запятую")
    org_codes: Annotated[list[str], NoDecode] = Field(description="Коды организации через запятую")
    database_url: str = Field(description="postgresql+psycopg://пользователь:пароль@хост/база")
    time_zone: str = "Europe/Moscow"
    catchup_hours: int = Field(default=24, ge=1, le=24 * 30)
    backup_status_file: str = "/backups/status.json"
    log_level: str = "INFO"

    @field_validator("messenger_url")
    @classmethod
    def _url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("MESSENGER_URL должен начинаться с http:// или https://")
        return url

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _owners(cls, value: object) -> list[int]:
        items = [part.strip() for part in str(value).split(",") if part.strip()]
        if not items:
            raise ValueError("OWNER_IDS: укажите хотя бы один ID владельца")
        owners: list[int] = []
        for item in items:
            if not item.isdigit() or int(item) <= 0:
                raise ValueError(f"OWNER_IDS: «{item}» не числовой ID пользователя")
            owners.append(int(item))
        return owners

    @field_validator("org_codes", mode="before")
    @classmethod
    def _orgs(cls, value: object) -> list[str]:
        items = [part.strip() for part in str(value).split(",") if part.strip()]
        if not items:
            raise ValueError("ORG_CODES: укажите хотя бы один код организации")
        codes: list[str] = []
        for item in items:
            if len(item) != 4 or not all(text.is_russian_capital(c) for c in item):
                raise ValueError(
                    f"ORG_CODES: «{item}» не код организации: нужны 4 заглавные русские буквы"
                )
            if item in codes:
                raise ValueError(f"ORG_CODES: код «{item}» указан дважды")
            codes.append(item)
        return codes

    @field_validator("time_zone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"TIME_ZONE: неизвестный часовой пояс «{value}»") from error
        return value

    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)
