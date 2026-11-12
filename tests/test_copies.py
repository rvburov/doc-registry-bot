"""Резервная копия и замена журнала через чат: права, папки, итог по файлам, отказы.

Контейнер копий — подделка (tests/fake_backup.py), снимок базы и заявки — настоящие.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot import backups, importer
from doc_registry_bot.bot import commands
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import Attachment
from doc_registry_bot.db.models import AuditEntry, BackupJob, JournalUpload, Member
from doc_registry_bot.registry import instructions, markup, schedule
from doc_registry_bot.registry.folders import SHARE, TERMINAL, Choice, Folder
from tests.fake_backup import FakeContainer
from tests.stand import ADMIN, OWNER, REGISTRAR, Stand

pytestmark = pytest.mark.asyncio

SECOND_OWNER = 3
FOLDERS = (
    Folder("1", "Основная", "/mnt/e/Копии"),
    Folder("2", "Архив", "/srv/архив"),
    Folder(SHARE, "Сетевая папка", "//fileserver/копии"),
)
HEADER = ";".join(importer.schema.COLUMNS)


@pytest.fixture
def owners_config(config: BotConfig) -> BotConfig:
    return replace(
        config,
        owner_ids=(OWNER, SECOND_OWNER),
        folders=FOLDERS,
        reminder=schedule.Weekly(0, time(10, 0), ZoneInfo("Europe/Moscow")),
    )


@pytest_asyncio.fixture
async def container(
    db: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FakeContainer]:
    monkeypatch.setattr(backups, "POLL_SECONDS", 0.01)
    fake = FakeContainer(db, tmp_path / "copies")
    stop = asyncio.Event()
    runner = asyncio.create_task(fake.run(stop))
    yield fake
    stop.set()
    await runner
    assert fake.errors == []


@pytest_asyncio.fixture
async def stand(db: async_sessionmaker[AsyncSession], owners_config: BotConfig) -> Stand:
    result = Stand(db, owners_config)
    result.messenger.person(SECOND_OWNER, "Вторых", "Виктор", "Викторович")
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    async with db() as session, session.begin():
        session.add(Member(user_id=OWNER, fio="Владельцев Владимир Владимирович"))
        session.add(Member(user_id=SECOND_OWNER, fio="Вторых Виктор Викторович"))
    await result.add("АБВГ.301121.001", "АБВГ.301121.002")
    return result


async def jobs(stand: Stand) -> list[BackupJob]:
    async with stand.db() as session:
        return list(await session.scalars(select(BackupJob).order_by(BackupJob.id)))


async def make_backup(stand: Stand, answer: str = "Да") -> str:
    await stand.say(OWNER, "Резервная копия")
    return await stand.say(OWNER, answer)


# ── Права и инструкции ────────────────────────────────────────────────────────


@pytest.mark.parametrize("user", [ADMIN, REGISTRAR])
async def test_only_the_main_admin_can_back_up_or_replace(stand: Stand, user: int) -> None:
    for command in ("Резервная копия", "Заменить журнал", "Инструкция главного администратора"):
        answer = await stand.say(user, command)
        assert markup.plain(answer) == commands.NO_RIGHTS
    assert await jobs(stand) == []


async def test_three_instructions_and_help_for_the_main_admin(stand: Stand) -> None:
    assert await stand.say(OWNER, "Инструкция главного администратора") == instructions.OWNER_TEXT
    owner_help = markup.plain(await stand.say(OWNER, "Помощь"))
    assert "Резервная копия" in owner_help and "Заменить журнал" in owner_help
    admin_help = markup.plain(await stand.say(ADMIN, "Помощь"))
    assert "Резервная копия" not in admin_help
    assert "Инструкция главного администратора" not in admin_help


# ── Резервная копия ───────────────────────────────────────────────────────────


async def test_the_backup_shows_the_default_folder_and_writes_both_files(
    stand: Stand, container: FakeContainer
) -> None:
    prompt = markup.plain(await stand.say(OWNER, "Резервная копия"))
    assert "Папка: Основная (E:\\Копии)" in prompt
    assert "1. Основная (E:\\Копии)\n2. Архив (/srv/архив)\n3. Сетевая папка" in prompt
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert answer.startswith("Резервная копия делается\nПапка: Основная (E:\\Копии)")
    assert "Резервная копия сделана\nПапка: Основная (E:\\Копии)" in answer
    [job] = await jobs(stand)
    assert job.successful and job.rows == 2 and job.source == "chat"
    assert f"DUMP базы: создан, {job.dump_name}" in answer
    assert f"CSV журнала: создан, {job.csv_name}" in answer
    folder = container.folder("1", "")
    assert (folder / job.dump_name).exists() and (folder / job.csv_name).exists()
    assert job.reported_at is not None
    async with stand.db() as session:
        actions = list(await session.scalars(select(AuditEntry.action)))
    assert "Резервная копия" in actions


async def test_another_folder_by_number_or_path_is_asked_again(
    stand: Stand, container: FakeContainer
) -> None:
    await stand.say(OWNER, "Резервная копия")
    again = markup.plain(await stand.say(OWNER, "2"))
    assert "Папка: Архив (/srv/архив)" in again
    await stand.say(OWNER, "Да")
    await stand.say(OWNER, "Резервная копия")
    sub = markup.plain(await stand.say(OWNER, "E:\\Копии\\2026\\ноябрь"))
    assert "Папка: Основная (E:\\Копии\\2026\\ноябрь)" in sub
    await stand.say(OWNER, "да")
    first, second = await jobs(stand)
    assert first.folder_key == "2" and first.successful
    assert second.folder_key == "1" and second.subdir == "2026/ноябрь" and second.successful
    assert (container.folder("1", "2026/ноябрь") / second.csv_name).exists()


async def test_a_folder_outside_the_allowed_list_is_refused_clearly(
    stand: Stand, container: FakeContainer
) -> None:
    await stand.say(OWNER, "Резервная копия")
    for path in ("D:\\Другое", "/etc", "E:\\Копии\\..\\Windows"):
        answer = markup.plain(await stand.say(OWNER, path))
        assert answer.startswith("Папка") or "недоступна" in answer, answer
    answer = markup.plain(await stand.say(OWNER, "D:\\Другое"))
    assert "недоступна боту" in answer and "BACKUP_DIR_2" in answer
    assert markup.plain(await stand.say(OWNER, "Нет")).startswith("Операция отменена")
    assert await jobs(stand) == []


async def test_windows_paths_ignore_letter_case_and_the_bot_folder_is_known(
    stand: Stand, container: FakeContainer
) -> None:
    from doc_registry_bot.registry.folders import choose

    folders = [Folder("1", "Основная", "/mnt/e/bot/deploy/backups"), *FOLDERS[1:]]
    for typed in (
        "e:\\BOT\\Deploy\\backups",
        "/mnt/e/bot/deploy/backups/",
        "E:/bot/deploy/backups",
    ):
        found = choose(folders, typed)
        assert isinstance(found, Choice) and found.folder.key == "1", typed
    sub = choose(folders, "E:\\Bot\\Deploy\\Backups\\2026")
    assert isinstance(sub, Choice) and sub.subdir == "2026"
    assert choose(folders, "\\\\FILESERVER\\копии") == Choice(FOLDERS[2])
    linux = choose([Folder("2", "Архив", "/srv/архив")], "/SRV/архив")
    assert isinstance(linux, str)


async def test_the_folder_can_be_given_right_in_the_command(
    stand: Stand, container: FakeContainer
) -> None:
    prompt = markup.plain(await stand.say(OWNER, "Резервная копия: Архив"))
    assert "Папка: Архив (/srv/архив)" in prompt
    await stand.say(OWNER, "Да")
    [job] = await jobs(stand)
    assert job.folder_key == "2"


async def test_one_failed_file_means_no_backup(stand: Stand, container: FakeContainer) -> None:
    container.fail_csv = True
    answer = markup.plain(await make_backup(stand))
    assert "Резервная копия не сделана" in answer
    assert "DUMP базы: создан" in answer
    assert "CSV журнала: не создан" in answer
    assert "только если созданы оба файла" in answer
    container.fail_csv = False
    container.fail_dump = True
    answer = markup.plain(await make_backup(stand))
    assert "DUMP базы: не создан" in answer and "CSV журнала: не создан" in answer
    assert "Причина: DUMP: pg_dump: ошибка" in answer
    assert not any(job.successful for job in await jobs(stand))


async def test_a_folder_the_container_does_not_have_is_reported(
    stand: Stand, container: FakeContainer
) -> None:
    container.configured = {"0", "1"}
    await stand.say(OWNER, "Резервная копия")
    await stand.say(OWNER, "2")
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert "Резервная копия не сделана" in answer
    assert "не подключена к контейнеру копий" in answer


async def test_a_stopped_container_cancels_the_request(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backups, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(backups, "PICKUP_SECONDS", 0.05)
    answer = markup.plain(await make_backup(stand))
    assert "Резервная копия не сделана" in answer
    assert "контейнер копий не ответил" in answer
    [job] = await jobs(stand)
    assert job.status == "canceled"


async def test_a_request_closed_before_the_container_got_it_is_not_made(
    stand: Stand, container: FakeContainer
) -> None:
    # Заявку из чата закрыли (например, разбор после перезапуска) раньше, чем бот снял снимок.
    async with stand.db() as session, session.begin():
        job = BackupJob(
            kind="backup",
            source="chat",
            requested_by=OWNER,
            folder_key="1",
            status="canceled",
            created_at=stand.now,
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    request = backups.Request(Choice(FOLDERS[0]), "chat", OWNER, "Владельцев В. В.")
    with pytest.raises(RuntimeError, match="уже не ждет выполнения"):
        await backups.make(stand.db, ZoneInfo("Europe/Moscow"), request, stand.now, job_id=job_id)
    [closed] = await jobs(stand)
    assert closed.status == "canceled" and closed.snapshot is None
    assert container.done == 0


async def test_dump_and_csv_are_taken_at_the_same_moment(
    stand: Stand, container: FakeContainer
) -> None:
    async def register() -> None:
        await stand.add("АБВГ.301121.003")

    container.before_dump = register
    await make_backup(stand)
    [job] = await jobs(stand)
    folder = container.folder("1", "")
    dumped = json.loads((folder / job.dump_name).read_text(encoding="utf-8"))
    csv_rows = (folder / job.csv_name).read_bytes().decode("utf-8-sig").strip().split("\r\n")[1:]
    assert dumped == ["АБВГ.301121.001", "АБВГ.301121.002"]
    assert [line.split(";")[1] for line in csv_rows] == dumped
    assert len(await stand.designations()) == 3


async def test_only_the_initiator_confirms(stand: Stand, container: FakeContainer) -> None:
    await stand.say(OWNER, "Резервная копия")
    other = markup.plain(await stand.say(SECOND_OWNER, "Да"))
    assert other.startswith("Подтверждение: нет операции")
    assert await jobs(stand) == []
    await stand.say(OWNER, "Да")
    assert len(await jobs(stand)) == 1


async def test_a_repeated_yes_does_not_make_a_second_backup(
    stand: Stand, container: FakeContainer
) -> None:
    await stand.say(OWNER, "Резервная копия")
    await stand.say(OWNER, "Да", message_id=777)
    assert await stand.say(OWNER, "Да", message_id=777) == ""
    assert len(await jobs(stand)) == 1


# ── Замена журнала ───────────────────────────────────────────────────────────


def journal_file(*lines: str) -> bytes:
    return "\r\n".join([HEADER, *lines]).encode("utf-8-sig")


NEW_JOURNAL = journal_file(
    "Вал;АБВГ.301121.010;Иванов Иван Иванович;01.02.2024 10:30;Машина;;;",
    "Ось;АБВГ.301121.011;Петров Петр Петрович;;Станок;;;",
    "Втулка;АБВГ.301121.012;Петров Петр Петрович;;Станок;;;",
)


async def send_journal(stand: Stand, data: bytes = NEW_JOURNAL, name: str = "журнал.csv") -> str:
    stand.message_id += 1
    stand.messenger.files[(stand.message_id, 1)] = Attachment(name, data)
    return await stand.say(OWNER, "Заменить журнал", files=[1], message_id=stand.message_id)


async def uploads(stand: Stand) -> list[JournalUpload]:
    async with stand.db() as session:
        return list(await session.scalars(select(JournalUpload).order_by(JournalUpload.id)))


async def test_the_journal_is_replaced_after_a_fresh_backup(
    stand: Stand, container: FakeContainer
) -> None:
    await make_backup(stand)
    prompt = markup.plain(await send_journal(stand))
    assert "Файл: журнал.csv, записей: 3. Сейчас в журнале: 2." in prompt
    assert "Весь журнал КД будет заменен записями из файла." in prompt
    assert "Доступы, администраторы, журнал действий" in prompt
    done = markup.plain(await stand.say(OWNER, "Да"))
    assert "Замена журнала начата" in done
    assert "Журнал КД заменен\nБыло записей: 2, стало: 3." in done
    assert await stand.designations() == ["АБВГ.301121.010", "АБВГ.301121.011", "АБВГ.301121.012"]
    admin = await stand.member(ADMIN)
    assert admin is not None and admin.is_admin
    [upload] = await uploads(stand)
    assert upload.status == "done"
    async with stand.db() as session:
        actions = list(await session.scalars(select(AuditEntry.action)))
    assert "Замена журнала из файла через чат" in actions
    verify = [job for job in await jobs(stand) if job.kind == "verify"]
    assert len(verify) == 1 and verify[0].status == "done"


async def test_no_fresh_backup_no_replacement(stand: Stand, container: FakeContainer) -> None:
    answer = markup.plain(await send_journal(stand))
    assert "нет свежей резервной копии" in answer
    await make_backup(stand)
    stand.later(31)
    answer = markup.plain(await send_journal(stand))
    assert "нет свежей резервной копии" in answer
    assert len(await stand.designations()) == 2
    assert await uploads(stand) == []


async def test_a_failed_backup_does_not_count(stand: Stand, container: FakeContainer) -> None:
    container.fail_csv = True
    await make_backup(stand)
    answer = markup.plain(await send_journal(stand))
    assert "нет свежей резервной копии" in answer


async def test_a_journal_changed_after_the_backup_is_not_replaced(
    stand: Stand, container: FakeContainer
) -> None:
    await make_backup(stand)
    await stand.add("АБВГ.301121.003")
    answer = markup.plain(await send_journal(stand))
    assert "журнал изменился после резервной копии" in answer


async def test_a_registration_between_the_warning_and_yes_stops_the_replacement(
    stand: Stand, container: FakeContainer
) -> None:
    await make_backup(stand)
    await send_journal(stand)
    await stand.add("АБВГ.301121.003")
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert "Журнал не заменен, в базе все осталось как было" in answer
    assert "журнал изменился после резервной копии" in answer
    assert len(await stand.designations()) == 3
    [upload] = await uploads(stand)
    assert upload.status == "refused"


async def test_the_backup_is_checked_again_right_before_the_replacement(
    stand: Stand, container: FakeContainer
) -> None:
    await make_backup(stand)
    stand.later(25)
    await send_journal(stand)
    stand.later(6)
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert "резервная копия устарела" in answer
    assert len(await stand.designations()) == 2


async def test_the_backup_expiring_during_the_file_check_stops_the_replacement(
    stand: Stand, container: FakeContainer
) -> None:
    """Свежесть проверяется еще раз уже под блокировкой журнала, после проверки файлов."""
    await make_backup(stand)
    stand.later(29)
    await send_journal(stand)

    async def slow_check() -> None:
        stand.later(2)

    container.before_verify = slow_check
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert "резервная копия устарела" in answer
    assert len(await stand.designations()) == 2


async def test_only_a_backup_with_both_files_counts(stand: Stand) -> None:
    async with stand.db() as session, session.begin():
        for status, dump_ok, csv_ok in (("done", False, True), ("failed", True, True)):
            session.add(
                BackupJob(
                    kind="backup",
                    status=status,
                    dump_ok=dump_ok,
                    csv_ok=csv_ok,
                    created_at=stand.now,
                )
            )
    async with stand.db() as session:
        assert await backups.latest_successful(session, stand.now) is None


async def test_a_damaged_backup_is_refused(stand: Stand, container: FakeContainer) -> None:
    await make_backup(stand)
    [job] = await jobs(stand)
    (container.folder("1", "") / job.csv_name).write_bytes(b"other")
    await send_journal(stand)
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert "резервная копия повреждена или недоступна" in answer
    assert len(await stand.designations()) == 2


async def test_a_terminal_backup_is_accepted(stand: Stand, container: FakeContainer) -> None:
    request = backups.Request(
        Choice(Folder(TERMINAL, "Копии из терминала", "./backups")), source="terminal"
    )
    job = await backups.make(stand.db, stand.dispatcher._config.zone, request, stand.now)
    assert job.successful
    await send_journal(stand)
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert "Журнал КД заменен" in answer


async def test_no_and_silence_change_nothing(stand: Stand, container: FakeContainer) -> None:
    await make_backup(stand)
    await send_journal(stand)
    assert markup.plain(await stand.say(OWNER, "Нет")).startswith("Операция отменена")
    await send_journal(stand)
    stand.later(11)
    expired = markup.plain(await stand.say(OWNER, "Да"))
    assert "срок истек" in expired
    assert len(await stand.designations()) == 2
    statuses = [upload.status for upload in await uploads(stand)]
    assert statuses == ["canceled", "waiting"]


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("журнал.xlsx", NEW_JOURNAL, "нужен файл .csv"),
        ("журнал.csv", journal_file("Вал;;;;;;;"), "пустое «Обозначение»"),
        ("журнал.csv", b"a;b\r\n1;2", "В файле нет столбцов"),
    ],
)
async def test_wrong_files_are_refused(
    stand: Stand, container: FakeContainer, name: str, data: bytes, expected: str
) -> None:
    await make_backup(stand)
    answer = markup.plain(await send_journal(stand, data, name))
    assert expected in answer
    assert await uploads(stand) == []


async def test_without_a_file_the_command_explains(stand: Stand) -> None:
    answer = markup.plain(await stand.say(OWNER, "Заменить журнал"))
    assert "приложите к этому же сообщению один файл CSV" in answer


async def test_two_main_admins_cannot_both_replace_from_one_backup(
    stand: Stand, container: FakeContainer
) -> None:
    await make_backup(stand)
    await send_journal(stand)
    stand.message_id += 1
    stand.messenger.files[(stand.message_id, 1)] = Attachment(
        "другой.csv", journal_file("Шайба;АБВГ.301121.050;Иванов Иван Иванович;;Машина;;;")
    )
    await stand.say(SECOND_OWNER, "Заменить журнал", files=[1], message_id=stand.message_id)
    first = markup.plain(await stand.say(OWNER, "Да"))
    second = markup.plain(await stand.say(SECOND_OWNER, "Да"))
    assert "Журнал КД заменен" in first
    assert "журнал изменился после резервной копии" in second
    assert len(await stand.designations()) == 3


async def test_backup_records_are_kept(stand: Stand, container: FakeContainer) -> None:
    await make_backup(stand)
    stand.later(60 * 24 * 400)
    async with stand.db() as session:
        assert await session.scalar(select(BackupJob.id)) is not None
    assert timedelta(minutes=30) == importer.FRESH


async def test_a_registration_during_the_replacement_waits_and_takes_a_free_number(
    stand: Stand,
) -> None:
    """Замена держит журнал (EXCLUSIVE); регистрация ждет и выбирает номер по новому журналу,
    а не падает на повторе номера."""
    from sqlalchemy import delete, text

    from doc_registry_bot.db import journal
    from doc_registry_bot.db.models import JournalEntry
    from tests.stand import registration

    async with stand.db() as session:
        await session.begin()
        await session.execute(text("LOCK TABLE journal IN EXCLUSIVE MODE"))
        await session.execute(delete(JournalEntry))
        await journal.add_entries(
            session,
            [
                journal.NewEntry(f"АБВГ.301121.00{n}", "Вал", "Иванов Иван Иванович", "Машина")
                for n in (1, 2, 3)
            ],
            stand.now,
        )
        registering = asyncio.create_task(stand.say(OWNER, registration()))
        await asyncio.sleep(0.3)
        assert not registering.done()
        await session.commit()
    answer = markup.plain(await registering)
    assert answer.startswith("Зарегистрировано"), answer
    assert "АБВГ.301121.004" in answer
