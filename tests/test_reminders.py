"""Еженедельное напоминание о резервной копии и разбор дел после перезапуска бота."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot import backups
from doc_registry_bot.bot import copies, pending
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.db.models import BackupJob, JournalUpload, Member, Reminder
from doc_registry_bot.registry import markup, schedule
from doc_registry_bot.registry.folders import Folder
from doc_registry_bot.service import BotService
from tests.fake_backup import FakeContainer
from tests.stand import OWNER, Stand

pytestmark = pytest.mark.asyncio

SECOND = 3
MOSCOW = ZoneInfo("Europe/Moscow")
# Вторник 10.11.2026 12:00 по Москве; срок напоминания — понедельник 10:00 по Москве.
START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
NEXT_SLOT = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)


@pytest.fixture
def reminder_config(config: BotConfig) -> BotConfig:
    return replace(
        config,
        owner_ids=(OWNER, SECOND),
        folders=(Folder("1", "Основная", "/mnt/e/Копии"),),
        reminder=schedule.Weekly(0, time(10, 0), MOSCOW),
    )


@pytest_asyncio.fixture
async def stand(db: async_sessionmaker[AsyncSession], reminder_config: BotConfig) -> Stand:
    result = Stand(db, reminder_config)
    result.messenger.person(SECOND, "Вторых", "Виктор", "Викторович")
    async with db() as session, session.begin():
        session.add(Member(user_id=OWNER, fio="Владельцев Владимир Владимирович"))
        session.add(Member(user_id=SECOND, fio="Вторых Виктор Викторович"))
    return result


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def service_of(stand: Stand, config: BotConfig, clock: Clock) -> BotService:
    service = BotService(stand.db, config, stand.messenger)  # type: ignore[arg-type]
    service.clock = clock
    return service


def reminders_to(stand: Stand, user: int) -> list[str]:
    return [
        markup.plain(sent.html)
        for sent in stand.messenger.sent
        if sent.user_id == user and copies.REMINDER_TEXT in markup.plain(sent.html)
    ]


async def rows(stand: Stand) -> list[Reminder]:
    async with stand.db() as session:
        return list(await session.scalars(select(Reminder).order_by(Reminder.id)))


async def first_run(stand: Stand, config: BotConfig) -> tuple[BotService, Clock]:
    clock = Clock(START)
    service = service_of(stand, config, clock)
    await service.remind()
    return service, clock


async def test_the_slot_is_the_last_monday_ten_oclock_in_moscow() -> None:
    weekly = schedule.Weekly(0, time(10, 0), MOSCOW)
    assert weekly.slot(START) == datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    assert weekly.slot(NEXT_SLOT) == NEXT_SLOT
    assert weekly.slot(NEXT_SLOT - timedelta(seconds=1)) == datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    assert schedule.parse_day("пятница") == 4
    assert schedule.parse_day("7") == 6
    assert schedule.parse_day("нет") is None
    berlin = schedule.Weekly(0, time(10, 0), ZoneInfo("Europe/Berlin"))
    # 25.10.2026 Германия перешла на зимнее время: следующий срок в 10:00 по местному времени,
    # а не ровно через 7 суток.
    assert berlin.following(datetime(2026, 10, 19, 8, 30, tzinfo=UTC)) == datetime(
        2026, 10, 26, 9, 0, tzinfo=UTC
    )


async def test_the_first_start_does_not_count_older_slots(
    stand: Stand, reminder_config: BotConfig
) -> None:
    await first_run(stand, reminder_config)
    assert stand.messenger.sent == []


async def test_every_owner_gets_one_reminder_and_restart_does_not_repeat_it(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    clock.moment = NEXT_SLOT + timedelta(minutes=1)
    await service.remind()
    await service.remind()
    restarted = service_of(stand, reminder_config, clock)
    await restarted.remind()
    assert reminders_to(stand, OWNER) == [copies.REMINDER_TEXT]
    assert reminders_to(stand, SECOND) == [copies.REMINDER_TEXT]
    assert {row.status for row in await rows(stand)} == {"sent"}


async def test_weeks_missed_while_the_bot_was_off_give_one_reminder(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    clock.moment = NEXT_SLOT + timedelta(days=17)
    await service.remind()
    clock.moment += timedelta(hours=5)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1
    [slot] = {row.slot_at for row in await rows(stand)}
    assert slot == NEXT_SLOT + timedelta(days=14)


async def test_a_failed_delivery_is_not_counted_and_is_retried(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    stand.messenger.broken = {OWNER}
    clock.moment = NEXT_SLOT
    await service.remind()
    mine = next(row for row in await rows(stand) if row.owner_id == OWNER)
    assert mine.status == "failed" and mine.sent_at is None
    stand.messenger.broken = set()
    clock.moment += timedelta(minutes=1)
    await service.remind()
    assert reminders_to(stand, OWNER) == []
    clock.moment += timedelta(minutes=10)
    await service.remind()
    assert reminders_to(stand, OWNER) == [copies.REMINDER_TEXT]
    assert len(reminders_to(stand, SECOND)) == 1


async def test_a_send_cut_by_a_crash_is_sent_again(
    stand: Stand, reminder_config: BotConfig
) -> None:
    _, clock = await first_run(stand, reminder_config)
    async with stand.db() as session, session.begin():
        session.add(Reminder(slot_at=NEXT_SLOT, owner_id=OWNER, status="sending", attempts=1))
    clock.moment = NEXT_SLOT + timedelta(minutes=2)
    await service_of(stand, reminder_config, clock).remind()
    assert reminders_to(stand, OWNER) == [copies.REMINDER_TEXT]


async def test_an_owner_in_the_middle_of_an_operation_is_reminded_later(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    stand.now = NEXT_SLOT
    await stand.say(OWNER, "Резервная копия")
    clock.moment = NEXT_SLOT + timedelta(minutes=1)
    await service.remind()
    assert reminders_to(stand, OWNER) == []
    assert len(reminders_to(stand, SECOND)) == 1
    stand.now = clock.moment
    await stand.say(OWNER, "Нет")
    clock.moment += timedelta(minutes=1)
    await service.remind()
    assert reminders_to(stand, OWNER) == [copies.REMINDER_TEXT]


async def remind_both(stand: Stand, config: BotConfig) -> tuple[BotService, Clock]:
    service, clock = await first_run(stand, config)
    clock.moment = NEXT_SLOT
    await service.remind()
    stand.now = NEXT_SLOT + timedelta(minutes=5)
    return service, clock


def terminal_copy(at: datetime) -> BackupJob:
    """Успешная копия из терминала (./backup.sh), начатая в `at`."""
    return BackupJob(
        kind="backup",
        source="terminal",
        folder_key="0",
        status="done",
        dump_ok=True,
        csv_ok=True,
        created_at=at,
        reported_at=at,
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


async def test_yes_leads_to_the_folder_and_tells_the_other_owner(
    stand: Stand, reminder_config: BotConfig, container: FakeContainer
) -> None:
    await remind_both(stand, reminder_config)
    prompt = markup.plain(await stand.say(OWNER, "Да"))
    assert "Папка: Основная (E:\\Копии)" in prompt
    others = [markup.plain(r.html) for r in stand.replies if r.user_id == SECOND]
    assert others == ["Резервную копию по напоминанию делает Владельцев Владимир Владимирович."]
    done = markup.plain(await stand.say(OWNER, "Да"))
    assert "Резервная копия сделана" in done
    late = markup.plain(await stand.say(SECOND, "Да"))
    assert late.startswith("На напоминание уже ответили")
    async with stand.db() as session:
        [job] = list(await session.scalars(select(BackupJob)))
    assert job.source == "reminder" and job.successful


async def test_no_closes_the_reminder_for_everybody(
    stand: Stand, reminder_config: BotConfig
) -> None:
    await remind_both(stand, reminder_config)
    answer = markup.plain(await stand.say(SECOND, "Нет"))
    assert answer.startswith("Резервная копия не делается\nСледующее напоминание: 23.11.2026 10:00")
    note = [markup.plain(r.html) for r in stand.replies if r.user_id == OWNER]
    assert note == [
        "Вторых Виктор Викторович: ответ Нет на напоминание, копия на этой неделе не делается."
    ]
    assert all(row.answer == "нет" and row.closed_at for row in await rows(stand))
    async with stand.db() as session:
        assert list(await session.scalars(select(BackupJob))) == []


async def test_an_answer_after_a_day_is_not_taken(stand: Stand, reminder_config: BotConfig) -> None:
    await remind_both(stand, reminder_config)
    stand.later(25 * 60)
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert answer.startswith("Подтверждение: нет операции")


async def test_a_yes_written_before_the_reminder_is_not_an_answer(
    stand: Stand, reminder_config: BotConfig
) -> None:
    await remind_both(stand, reminder_config)
    written = int((NEXT_SLOT - timedelta(minutes=3)).timestamp() * 1000)
    answer = markup.plain(await stand.say(OWNER, "Да", ctime=written))
    assert answer.startswith("Подтверждение: нет операции")
    assert all(row.closed_at is None for row in await rows(stand))


async def test_a_closed_reminder_is_not_retried_for_the_others(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    stand.messenger.broken = {SECOND}
    clock.moment = NEXT_SLOT
    await service.remind()
    stand.now = NEXT_SLOT + timedelta(minutes=1)
    await stand.say(OWNER, "Нет")
    stand.messenger.broken = set()
    clock.moment += timedelta(minutes=30)
    await service.remind()
    assert reminders_to(stand, SECOND) == []


async def test_no_reminders_when_turned_off(stand: Stand, reminder_config: BotConfig) -> None:
    config = replace(reminder_config, reminder=None)
    clock = Clock(START)
    service = service_of(stand, config, clock)
    await service.remind()
    clock.moment = NEXT_SLOT + timedelta(days=30)
    await service.remind()
    assert stand.messenger.sent == []


async def test_after_a_restart_interrupted_work_is_reported(
    stand: Stand, reminder_config: BotConfig
) -> None:
    async with stand.db() as session, session.begin():
        session.add(
            BackupJob(
                kind="backup",
                source="chat",
                requested_by=OWNER,
                folder_key="1",
                folder_label="Основная (E:\\Копии)",
                status="new",
                created_at=START,
            )
        )
        session.add(
            JournalUpload(
                user_id=SECOND,
                created_at=START,
                name="журнал.csv",
                data=b"x",
                sha256="x",
                status="confirmed",
            )
        )
    service = service_of(stand, reminder_config, Clock(START + timedelta(minutes=1)))
    await service.recover()
    texts = {sent.user_id: markup.plain(sent.html) for sent in stand.messenger.sent}
    assert texts[OWNER].startswith("Резервная копия не сделана")
    assert "бот перезапустился до начала копии" in texts[OWNER]
    assert texts[SECOND].startswith("Журнал не заменен, в базе все осталось как было")
    stand.messenger.sent.clear()
    await service.report()
    assert stand.messenger.sent == []


async def test_a_result_that_could_not_be_sent_is_sent_later(
    stand: Stand, reminder_config: BotConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with stand.db() as session, session.begin():
        session.add(
            BackupJob(
                kind="backup",
                source="chat",
                requested_by=OWNER,
                folder_key="1",
                status="failed",
                message="диск полон",
                created_at=START,
            )
        )
    service = service_of(stand, reminder_config, Clock(START))
    stand.messenger.broken = {OWNER}
    real_sleep = asyncio.sleep

    async def no_wait(_: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    await service.report()
    assert stand.messenger.sent == []
    stand.messenger.broken = set()
    await service.report()
    [sent] = stand.messenger.sent
    assert "диск полон" in markup.plain(sent.html)


async def test_a_new_schedule_does_not_send_an_extra_reminder(
    stand: Stand, reminder_config: BotConfig
) -> None:
    _, clock = await first_run(stand, reminder_config)
    clock.moment = NEXT_SLOT + timedelta(days=3)  # четверг: напоминание понедельника пропущено
    changed = replace(
        reminder_config, reminder=schedule.Weekly(2, time(9, 0), MOSCOW)
    )  # среда 09:00 — уже прошла на этой неделе
    moved = service_of(stand, changed, clock)
    await moved.remind()
    await moved.remind()
    assert reminders_to(stand, OWNER) == []
    clock.moment = datetime(2026, 11, 25, 6, 1, tzinfo=UTC)  # следующая среда 09:01 по Москве
    await moved.remind()
    assert reminders_to(stand, OWNER) == [copies.REMINDER_TEXT]


async def test_the_result_of_a_reminder_copy_goes_to_all_owners(
    stand: Stand, reminder_config: BotConfig, container: FakeContainer
) -> None:
    await remind_both(stand, reminder_config)
    await stand.say(OWNER, "Да")
    await stand.say(OWNER, "Да")
    notes = [markup.plain(r.html) for r in stand.replies if r.user_id == SECOND]
    assert notes == [
        "Резервная копия по напоминанию сделана. Делал: Владельцев Владимир Владимирович."
    ]


async def test_cancelling_a_reminder_copy_is_told_to_the_others(
    stand: Stand, reminder_config: BotConfig
) -> None:
    await remind_both(stand, reminder_config)
    await stand.say(OWNER, "Да")
    await stand.say(OWNER, "Нет")
    notes = [markup.plain(r.html) for r in stand.replies if r.user_id == SECOND]
    assert notes == ["Владельцев Владимир Владимирович: копия по напоминанию отменена, не сделана."]


async def test_a_copy_made_after_the_slot_makes_the_reminder_unneeded(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    async with stand.db() as session, session.begin():
        session.add(terminal_copy(NEXT_SLOT - timedelta(minutes=1)))
    clock.moment = NEXT_SLOT
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1  # копия до срока не в счет
    # Через неделю бот стоял в срок, а копию сделали в терминале до его запуска.
    week = NEXT_SLOT + timedelta(days=7)
    async with stand.db() as session, session.begin():
        session.add(terminal_copy(week + timedelta(minutes=30)))
    clock.moment = week + timedelta(hours=1)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1
    assert len(reminders_to(stand, SECOND)) == 1


async def test_a_late_yes_to_another_question_is_not_taken_for_the_reminder(
    stand: Stand, reminder_config: BotConfig
) -> None:
    await remind_both(stand, reminder_config)
    await stand.say(OWNER, "Резервная копия")  # вопрос о папке, ответа на него нет
    stand.later(70)
    async with stand.db() as session, session.begin():
        await pending.purge_expired(session, stand.now)  # чистка раз в час удалила ожидание
    answer = markup.plain(await stand.say(OWNER, "Да"))
    assert answer.startswith("Подтверждение: нет операции")
    assert all(row.closed_at is None for row in await rows(stand))


async def test_an_old_expired_question_does_not_take_the_answer_to_the_reminder(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    stand.now = NEXT_SLOT - timedelta(hours=1)
    await stand.say(OWNER, "Резервная копия")  # истек за 50 минут до напоминания
    clock.moment = NEXT_SLOT
    await service.remind()
    stand.now = NEXT_SLOT + timedelta(minutes=5)
    prompt = markup.plain(await stand.say(OWNER, "Да"))
    assert "Папка: Основная (E:\\Копии)" in prompt
    assert all(row.answer == "да" for row in await rows(stand))


async def test_after_another_question_the_reminder_is_repeated(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await first_run(stand, reminder_config)
    clock.moment = NEXT_SLOT
    await service.remind()
    stand.now = NEXT_SLOT + timedelta(hours=5)
    await stand.say(OWNER, "Резервная копия")
    clock.moment = stand.now + timedelta(minutes=1)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1  # пока ждет ответа на вопрос о папке
    stand.later(2)
    await stand.say(OWNER, "Нет")
    clock.moment = stand.now + timedelta(minutes=1)
    await service.remind()
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 2
    assert len(reminders_to(stand, SECOND)) == 1
    # Повтор — тоже напоминание: сутки на ответ считаются от него.
    stand.now = NEXT_SLOT + timedelta(hours=24, minutes=30)
    prompt = markup.plain(await stand.say(OWNER, "Да"))
    assert "Папка: Основная (E:\\Копии)" in prompt


async def test_the_reminder_is_repeated_only_during_the_first_day(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await remind_both(stand, reminder_config)

    async def another_question(at: datetime) -> None:
        stand.now = at
        await stand.say(OWNER, "Резервная копия")
        stand.later(1)
        await stand.say(OWNER, "Нет")
        clock.moment = stand.now + timedelta(minutes=1)
        await service.remind()

    await another_question(NEXT_SLOT + timedelta(hours=5))
    assert len(reminders_to(stand, OWNER)) == 2
    await another_question(NEXT_SLOT + timedelta(hours=25))
    assert len(reminders_to(stand, OWNER)) == 2


async def test_a_yes_without_a_copy_brings_the_reminder_back_to_everybody(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await remind_both(stand, reminder_config)
    stand.now = NEXT_SLOT + timedelta(hours=23, minutes=55)
    await stand.say(OWNER, "Да")  # вопрос о папке остался без ответа
    clock.moment = stand.now + timedelta(minutes=10)
    await service.remind()
    assert len(reminders_to(stand, SECOND)) == 1  # вопрос о папке еще можно было подтвердить
    clock.moment = stand.now + timedelta(minutes=12)
    await service.remind()
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 2
    assert len(reminders_to(stand, SECOND)) == 2
    stand.now = clock.moment + timedelta(minutes=1)
    answer = markup.plain(await stand.say(SECOND, "Нет"))
    assert answer.startswith("Резервная копия не делается")


async def test_a_folder_question_still_open_keeps_the_reminder_closed(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await remind_both(stand, reminder_config)
    answered = stand.now
    await stand.say(OWNER, "Да")
    stand.later(9)
    await stand.say(OWNER, "1")  # выбрал папку заново: вопрос о папке продлен
    clock.moment = answered + timedelta(minutes=12)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1
    assert len(reminders_to(stand, SECOND)) == 1


async def test_a_reminder_copy_still_running_keeps_the_reminder_closed(
    stand: Stand, reminder_config: BotConfig
) -> None:
    service, clock = await remind_both(stand, reminder_config)
    await stand.say(OWNER, "Да")
    async with stand.db() as session, session.begin():
        session.add(
            BackupJob(
                kind="backup",
                source="reminder",
                requested_by=OWNER,
                folder_key="1",
                status="running",
                created_at=stand.now,
            )
        )
    clock.moment = stand.now + timedelta(minutes=40)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1
    assert len(reminders_to(stand, SECOND)) == 1


async def test_a_failed_reminder_copy_brings_the_reminder_back(
    stand: Stand, reminder_config: BotConfig, container: FakeContainer
) -> None:
    service, clock = await remind_both(stand, reminder_config)
    container.fail_dump = True
    await stand.say(OWNER, "Да")
    done = markup.plain(await stand.say(OWNER, "Да"))
    assert "Резервная копия не сделана" in done
    clock.moment = stand.now + timedelta(minutes=12)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 2
    assert len(reminders_to(stand, SECOND)) == 2


async def test_a_successful_reminder_copy_closes_the_reminder(
    stand: Stand, reminder_config: BotConfig, container: FakeContainer
) -> None:
    service, clock = await remind_both(stand, reminder_config)
    await stand.say(OWNER, "Да")
    await stand.say(OWNER, "Да")
    clock.moment = stand.now + timedelta(minutes=12)
    await service.remind()
    clock.moment += timedelta(hours=2)
    await service.remind()
    assert len(reminders_to(stand, OWNER)) == 1
    assert len(reminders_to(stand, SECOND)) == 1


async def test_turning_the_reminder_off_and_on_gives_no_extra_reminder(
    stand: Stand, reminder_config: BotConfig
) -> None:
    _, clock = await first_run(stand, reminder_config)
    clock.moment = NEXT_SLOT - timedelta(days=1)
    await service_of(stand, replace(reminder_config, reminder=None), clock).remind()
    clock.moment = NEXT_SLOT + timedelta(days=2)  # срок понедельника прошел, пока было выключено
    again = service_of(stand, reminder_config, clock)
    await again.remind()
    await again.remind()
    assert stand.messenger.sent == []
    clock.moment = NEXT_SLOT + timedelta(days=7, minutes=1)
    await again.remind()
    assert reminders_to(stand, OWNER) == [copies.REMINDER_TEXT]


async def test_the_next_reminder_is_shown_in_the_time_of_the_schedule(
    stand: Stand, reminder_config: BotConfig
) -> None:
    ekaterinburg = ZoneInfo("Asia/Yekaterinburg")
    config = replace(reminder_config, reminder=schedule.Weekly(0, time(10, 0), ekaterinburg))
    zoned = Stand(stand.db, config)
    service, clock = await first_run(zoned, config)
    clock.moment = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)  # понедельник 10:00 в Екатеринбурге
    await service.remind()
    zoned.now = clock.moment + timedelta(minutes=5)
    answer = markup.plain(await zoned.say(OWNER, "Нет"))
    assert "Следующее напоминание: 23.11.2026 10:00 (Asia/Yekaterinburg)" in answer
