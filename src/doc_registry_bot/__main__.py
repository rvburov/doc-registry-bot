"""Запуск: `python -m doc_registry_bot [run | migrate | import ФАЙЛ.xlsx | version]`.

- `run` (по умолчанию) — обновить схему базы и запустить бота;
- `migrate` — только обновить схему базы;
- `import` — один раз перенести журнал из выгрузки SmartBase в пустую базу;
- `version` — версия программы.
"""

import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from doc_registry_bot import __version__
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.config import Settings
from doc_registry_bot.db.session import engine_for, sessions, upgrade
from doc_registry_bot.importer import import_journal
from doc_registry_bot.messenger.client import HttpMessenger
from doc_registry_bot.messenger.stream import EventStream
from doc_registry_bot.service import BotService

log = logging.getLogger("doc_registry_bot")


def _settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
    except ValidationError as error:
        for problem in error.errors():
            field = ".".join(str(part) for part in problem["loc"]).upper()
            message = str(problem["msg"]).removeprefix("Value error, ")
            print(f"Настройка {field}: {message}", file=sys.stderr)
        raise SystemExit(2) from error


def _config(settings: Settings) -> BotConfig:
    return BotConfig(
        owner_ids=tuple(settings.owner_ids),
        org_codes=tuple(settings.org_codes),
        zone=settings.zone,
        catchup_hours=settings.catchup_hours,
    )


async def _run(settings: Settings) -> None:
    engine = engine_for(settings.database_url)
    messenger = HttpMessenger(settings.messenger_url)
    bot_id: int | None = None
    try:
        me = await messenger.me()
        bot_id = me.user_id if me else None
    except Exception as error:
        log.warning("Карточка бота не получена (%s): свои сообщения отсеиваются по истории", error)
    service = BotService(
        sessions(engine),
        _config(settings),
        messenger,
        backup_status=Path(settings.backup_status_file),
        bot_id=bot_id,
    )
    stream = EventStream(settings.messenger_url, service.on_event, service.on_state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            loop.add_signal_handler(getattr(signal, name), stop.set)
    log.info("Бот регистрации КД, версия %s: запуск", __version__)
    try:
        await asyncio.gather(stream.run(stop), service.worker(stop), service.maintenance(stop))
    finally:
        await messenger.close()
        await engine.dispose()
        log.info("Бот остановлен")


async def _import(settings: Settings, data: bytes) -> int:
    engine = engine_for(settings.database_url)
    try:
        report = await import_journal(sessions(engine), data, settings.zone, datetime.now(UTC))
    finally:
        await engine.dispose()
    if report.ok:
        print(f"Журнал загружен: {report.rows} записей.")
        return 0
    print("Журнал не загружен. Исправьте ошибки и запустите загрузку заново:", file=sys.stderr)
    for problem in report.problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "run"
    if command in ("version", "--version"):
        print(__version__)
        return
    settings = _settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    zone = settings.zone
    # Время в журнале программы — по часовому поясу установки, а не UTC контейнера.
    logging.Formatter.converter = lambda *_: datetime.now(zone).timetuple()
    # Каждый запрос к службе мессенджера в журнал не пишется: только сбои.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if command in ("run", "migrate"):
        upgrade(settings.database_url)
        if command == "migrate":
            print("Схема базы обновлена.")
            return
        asyncio.run(_run(settings))
        return
    if command == "import":
        if len(args) != 2:
            print("Использование: import ФАЙЛ.xlsx", file=sys.stderr)
            raise SystemExit(2)
        upgrade(settings.database_url)
        raise SystemExit(asyncio.run(_import(settings, Path(args[1]).read_bytes())))
    print(f"Неизвестная команда: {command}. Можно: run, migrate, import, version", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
