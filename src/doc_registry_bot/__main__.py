"""Запуск: `python -m doc_registry_bot КОМАНДА`.

- `run` (по умолчанию) — обновить схему базы и запустить бота;
- `migrate` — только обновить схему базы;
- `export ФАЙЛ` — выгрузить журнал в CSV (`-` — в стандартный вывод), отпечаток — в stderr;
- `check ФАЙЛ` — проверить файл журнала (CSV или Excel), ничего не записывая;
- `import ФАЙЛ --expect ОТПЕЧАТОК` — заменить журнал файлом, только после резерва;
- `users ФАЙЛ.csv` — выдать доступ всем из файла (столбцы ID и ФИО);
- `contacts` — контакты учетной записи бота «ID;ФИО» в стандартный вывод (база не нужна);
- `backup [--uid N --gid N]` — резервная копия в папку backups рядом с compose.yaml через
  контейнер копий (DUMP и CSV одним снимком), итог в стандартный вывод;
- `settings` — проверить настройки из .env и ничего не делать (update.sh перед обновлением);
- `version` — версия программы.

Вместо ФАЙЛ в check, import и users можно `-`: файл со стандартного ввода, имя файла (по нему
определяется формат) — в `--name`. Так команды на сервере передают файл в контейнер без
подключения папок: deploy/backup.sh, import.sh, users.sh и contacts.sh.
"""

import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from doc_registry_bot import __version__, backups, importer
from doc_registry_bot.bot import copies
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.config import Settings
from doc_registry_bot.db.session import engine_for, sessions, upgrade
from doc_registry_bot.messenger.client import HttpMessenger
from doc_registry_bot.messenger.stream import EventStream
from doc_registry_bot.registry.folders import TERMINAL, Choice, Folder
from doc_registry_bot.service import BotService

log = logging.getLogger("doc_registry_bot")


def _settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
    except ValidationError as error:
        for problem in error.errors():
            field = ".".join(str(part) for part in problem["loc"]).upper()
            message = str(problem["msg"]).removeprefix("Value error, ").removeprefix(f"{field}: ")
            print(f"Настройка {field}: {message}", file=sys.stderr)
        raise SystemExit(2) from error


def _config(settings: Settings) -> BotConfig:
    return BotConfig(
        owner_ids=tuple(settings.owner_ids),
        org_codes=tuple(settings.org_codes),
        zone=settings.zone,
        catchup_hours=settings.catchup_hours,
        folders=settings.folders,
        reminder=settings.reminder,
    )


async def _run(settings: Settings) -> None:
    engine = engine_for(settings.dsn)
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


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _failed(heading: str, report: importer.Report) -> int:
    _say(heading)
    for problem in report.problems:
        _say(f"  {problem}")
    return 1


async def _export(settings: Settings) -> bytes:
    engine = engine_for(settings.dsn)
    try:
        async with sessions(engine)() as session:
            return await importer.export_csv(session, settings.zone)
    finally:
        await engine.dispose()


def _write_export(data: bytes, target: str) -> int:
    if target == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    else:
        Path(target).write_bytes(data)
    rows = max(0, data.decode("utf-8-sig").count("\r\n") - 1)
    _say(f"Выгружено записей: {rows}")
    _say(f"Отпечаток: {importer.fingerprint(data)}")
    return 0


def _input(args: list[str]) -> tuple[str, bytes]:
    """Имя и содержимое файла из аргументов: путь или `-` со стандартного ввода и `--name`."""
    if args[1] == "-":
        name = _option(args, "--name")
        if not name:
            _say("Для файла со стандартного ввода укажите имя: --name ИМЯ.csv")
            raise SystemExit(2)
        return Path(name).name, sys.stdin.buffer.read()
    path = Path(args[1])
    if not path.is_file():
        _say(f"Нет файла {path}")
        raise SystemExit(2)
    return path.name, path.read_bytes()


def _check(settings: Settings, name: str, data: bytes) -> int:
    _, report = importer.read_journal(data, name, settings.zone, datetime.now(UTC))
    if not report.ok:
        return _failed("В файле есть ошибки, загружать его нельзя:", report)
    _say(f"Файл годится, записей: {report.rows}.")
    return 0


async def _import(settings: Settings, name: str, data: bytes, expected: str | None) -> int:
    engine = engine_for(settings.dsn)
    try:
        report = await importer.replace_journal(
            sessions(engine),
            data,
            name,
            settings.zone,
            datetime.now(UTC),
            expected=expected,
        )
    finally:
        await engine.dispose()
    if not report.ok:
        return _failed("Журнал не заменен, в базе все осталось как было:", report)
    _say(f"Журнал заменен. Было записей: {report.before}, стало: {report.rows}.")
    return 0


async def _users(settings: Settings, name: str, data: bytes) -> int:
    engine = engine_for(settings.dsn)
    try:
        report = await importer.load_users(sessions(engine), data, name, datetime.now(UTC))
    finally:
        await engine.dispose()
    if not report.ok:
        return _failed("Пользователи не загружены, в базе все осталось как было:", report)
    _say(f"Доступ выдан, пользователей: {report.rows}. Инструкция не отправлялась.")
    return 0


async def _contacts(settings: Settings) -> int:
    messenger = HttpMessenger(settings.messenger_url)
    try:
        cards = await messenger.contacts()
    except Exception as error:
        _say(f"Ошибка: {error}")
        _say(f"Проверьте MESSENGER_URL в .env ({settings.messenger_url}) и сеть messenger.")
        return 1
    finally:
        await messenger.close()
    sys.stdout.buffer.write(importer.contacts_csv(cards))
    sys.stdout.flush()
    _say(f"Контактов: {len(cards)}")
    return 0


async def _backup(settings: Settings, uid: int | None, gid: int | None) -> int:
    """Копия из терминала: тот же путь, что из чата, папка backups рядом с compose.yaml."""
    engine = engine_for(settings.dsn)
    now = datetime.now(UTC)
    request = backups.Request(
        Choice(Folder(TERMINAL, "Копии из терминала", "./backups")),
        source="terminal",
        requested_fio=importer.SERVER_ACTOR,
        owner_uid=uid,
        owner_gid=gid,
    )
    try:
        db = sessions(engine)
        job = await backups.make(db, settings.zone, request, now)
        await backups.reported(db, job.id, now, importer.SERVER_ACTOR, copies.report_line(job))
    finally:
        await engine.dispose()
    if not job.successful:
        _say(f"Копия не сделана: {job.message or 'причина неизвестна'}")
        dump = "создан" if job.dump_ok else "не создан"
        table = "создан" if job.csv_ok else "не создан"
        _say(f"  DUMP: {dump}, CSV: {table}")
        return 1
    print(f"Журнал: {job.csv_name}")
    print(f"База: {job.dump_name}")
    print(f"Записей: {job.rows}")
    print(f"Отпечаток: {job.fingerprint}")
    return 0


def _number(args: list[str], name: str) -> int | None:
    value = _option(args, name)
    return int(value) if value is not None and value.isdigit() else None


def _option(args: list[str], name: str) -> str | None:
    if name in args:
        at = args.index(name)
        if at + 1 < len(args):
            return args[at + 1]
    return None


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "run"
    if command in ("version", "--version"):
        print(__version__)
        return
    settings = _settings()
    if command == "settings":
        print("Настройки в порядке.")
        return
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    zone = settings.zone
    # Время в журнале программы — по часовому поясу установки, а не UTC контейнера.
    logging.Formatter.converter = lambda *_: datetime.now(zone).timetuple()
    # Каждый запрос к службе мессенджера в журнал не пишется: только сбои.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if command != "run":
        # Команды на сервере пишут человеку: служебные строки миграций ему не нужны.
        logging.getLogger("alembic").setLevel(logging.WARNING)
    if command in ("run", "migrate"):
        upgrade(settings.dsn)
        if command == "migrate":
            print("Схема базы обновлена.")
            return
        asyncio.run(_run(settings))
        return
    if command == "contacts":
        raise SystemExit(asyncio.run(_contacts(settings)))
    if command == "backup":
        logging.getLogger("alembic").setLevel(logging.WARNING)
        upgrade(settings.dsn)
        uid, gid = _number(args, "--uid"), _number(args, "--gid")
        raise SystemExit(asyncio.run(_backup(settings, uid, gid)))
    if command in ("export", "check", "import", "users"):
        if len(args) < 2:
            _say(f"Укажите файл: {command} ФАЙЛ")
            raise SystemExit(2)
        if command == "export":
            upgrade(settings.dsn)
            raise SystemExit(_write_export(asyncio.run(_export(settings)), args[1]))
        name, data = _input(args)
        if command == "check":
            raise SystemExit(_check(settings, name, data))
        upgrade(settings.dsn)
        if command == "import":
            expected = _option(args, "--expect")
            raise SystemExit(asyncio.run(_import(settings, name, data, expected)))
        raise SystemExit(asyncio.run(_users(settings, name, data)))
    _say(
        f"Неизвестная команда: {command}. "
        "Можно: run, migrate, export, check, import, users, contacts, backup, version"
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
