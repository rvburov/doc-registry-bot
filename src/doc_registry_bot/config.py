"""Настройки установки — только из окружения (`.env` на сервере). Ошибка — бот не запускается.

Неверное значение должно остановить бота с понятной причиной в журнале: молча работающий бот с
неправильным кодом организации выдал бы номера не той серии.
"""

from functools import cached_property
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from doc_registry_bot.registry import schedule, text
from doc_registry_bot.registry.folders import SHARE, Folder

# Системные папки и корень: копии туда не пишутся.
_SYSTEM = {
    "bin",
    "boot",
    "dev",
    "etc",
    "home",
    "lib",
    "lib64",
    "mnt",
    "opt",
    "proc",
    "root",
    "run",
    "sbin",
    "srv",
    "sys",
    "tmp",
    "usr",
    "var",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    messenger_url: str = Field(description="Адрес службы мессенджера: http://контейнер:8888")
    owner_ids: Annotated[list[int], NoDecode] = Field(description="ID владельцев через запятую")
    org_codes: Annotated[list[str], NoDecode] = Field(description="Коды организации через запятую")
    # База бота: имя, пользователь и пароль из .env (их же получает контейнер PostgreSQL).
    # DATABASE_URL целиком — только если база не в нашем compose; тогда POSTGRES_* не нужны.
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "doc_registry"
    postgres_user: str = "doc_registry"
    postgres_password: str = ""
    database_url: str = ""
    time_zone: str = "Europe/Moscow"
    catchup_hours: int = Field(default=24, ge=1, le=24 * 30)
    backup_status_file: str = "/backups/status.json"
    log_level: str = "INFO"
    # Папки для резервных копий: пути на компьютере (как их понимает Docker) и имена для чата.
    # Пишет в них только контейнер копий, compose.yaml подключает к нему именно эти папки.
    backup_dir_1: str = "./backups"
    backup_dir_1_name: str = "Основная"
    backup_dir_2: str = ""
    backup_dir_2_name: str = "Папка 2"
    backup_dir_3: str = ""
    backup_dir_3_name: str = "Папка 3"
    # Сетевая папка Windows (//сервер/папка): ее путь только показывается в чате, пароль бот
    # не получает, файлы отправляет контейнер копий.
    backup_share: str = ""
    backup_share_name: str = "Сетевая папка"
    # Папка бота на компьютере (рядом с compose.yaml), ее пишут update.sh и backup.sh: по ней
    # путь ./backups показывается и узнается в чате как настоящий (E:\\...\\deploy\\backups).
    bot_dir: str = ""
    # Еженедельное напоминание о резервной копии.
    backup_reminder_day: str = "пн"
    backup_reminder_time: str = "10:00"
    backup_reminder_zone: str = ""

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

    @field_validator("backup_dir_1", "backup_dir_2", "backup_dir_3")
    @classmethod
    def _folder(cls, value: str, info: ValidationInfo) -> str:
        path = value.strip()
        name = str(info.field_name).upper()
        if not path:
            return path
        if ":" in path or "\\" in path:
            raise ValueError(
                f"{name}: путь для Docker в виде Ubuntu: /mnt/e/Копии (это E:\\Копии) или "
                "/srv/копии, без двоеточия и обратной косой черты"
            )
        if not path.startswith(("/", "./")):
            raise ValueError(f"{name}: путь должен начинаться с / или ./ (например, /mnt/e/Копии)")
        path = path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        whole_disk = parts[:1] == ["mnt"] and len(parts) <= 2
        if not parts or whole_disk or (len(parts) == 1 and parts[0] in _SYSTEM):
            raise ValueError(
                f"{name}: «{path}» — весь диск или системная папка; укажите отдельную папку для "
                "копий, например /mnt/e/Копии"
            )
        return path

    @field_validator("backup_reminder_day")
    @classmethod
    def _reminder_day(cls, value: str) -> str:
        schedule.parse_day(value)
        return value

    @field_validator("backup_reminder_time")
    @classmethod
    def _reminder_time(cls, value: str) -> str:
        schedule.parse_time(value)
        return value

    @field_validator("backup_reminder_zone")
    @classmethod
    def _reminder_zone(cls, value: str) -> str:
        if value.strip():
            try:
                ZoneInfo(value.strip())
            except ZoneInfoNotFoundError as error:
                raise ValueError(
                    f"BACKUP_REMINDER_ZONE: неизвестный часовой пояс «{value}»"
                ) from error
        return value.strip()

    @model_validator(mode="after")
    def _database(self) -> "Settings":
        if not self.database_url and not self.postgres_password:
            raise ValueError("POSTGRES_PASSWORD: задайте пароль базы бота")
        return self

    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)

    def _host(self, path: str) -> str:
        """Путь на компьютере: ./backups — внутри папки бота, если она известна."""
        if path.startswith("./") and self.bot_dir.startswith("/"):
            return self.bot_dir.rstrip("/") + path[1:]
        return path

    @property
    def folders(self) -> tuple[Folder, ...]:
        """Разрешенные папки для копий в порядке номеров в чате; сетевая папка последней."""
        found = [
            Folder(key, name.strip() or f"Папка {key}", self._host(path))
            for key, path, name in (
                ("1", self.backup_dir_1, self.backup_dir_1_name),
                ("2", self.backup_dir_2, self.backup_dir_2_name),
                ("3", self.backup_dir_3, self.backup_dir_3_name),
            )
            if path
        ]
        if self.backup_share.strip():
            found.append(
                Folder(SHARE, self.backup_share_name.strip() or "Сетевая папка", self.backup_share)
            )
        return tuple(found)

    @property
    def reminder(self) -> schedule.Weekly | None:
        weekday = schedule.parse_day(self.backup_reminder_day)
        if weekday is None:
            return None
        zone = ZoneInfo(self.backup_reminder_zone or self.time_zone)
        return schedule.Weekly(weekday, schedule.parse_time(self.backup_reminder_time), zone)

    @property
    def dsn(self) -> str:
        """Адрес базы для SQLAlchemy. Пароль экранируется: в нем могут быть @, : и /."""
        if self.database_url:
            return self.database_url
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        database = quote(self.postgres_db, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}@{self.postgres_host}:{self.postgres_port}"
            f"/{database}"
        )
