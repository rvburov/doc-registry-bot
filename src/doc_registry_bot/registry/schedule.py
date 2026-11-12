"""Еженедельное напоминание о резервной копии: день, время и часовой пояс из `.env`.

Напоминание привязано к «сроку» — последнему наступившему моменту по расписанию. Сколько бы
недель бот ни был выключен, после запуска важен только последний срок: пачки напоминаний за
каждую пропущенную неделю не будет.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from doc_registry_bot.registry.text import fold

DAYS = {
    "пн": 0,
    "понедельник": 0,
    "вт": 1,
    "вторник": 1,
    "ср": 2,
    "среда": 2,
    "чт": 3,
    "четверг": 3,
    "пт": 4,
    "пятница": 4,
    "сб": 5,
    "суббота": 5,
    "вс": 6,
    "воскресенье": 6,
}
OFF = ("нет", "выкл", "выключено", "off")
DAY_NAMES = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


@dataclass(frozen=True)
class Weekly:
    """Раз в неделю: день недели (0 — понедельник), время и часовой пояс."""

    weekday: int
    at: time
    zone: ZoneInfo

    def slot(self, now: datetime) -> datetime:
        """Последний наступивший срок, не позже `now`."""
        local = now.astimezone(self.zone)
        day = local.date() - timedelta(days=(local.weekday() - self.weekday) % 7)
        moment = _at(day, self.at, self.zone)
        if moment > now:
            moment = _at(day - timedelta(days=7), self.at, self.zone)
        return moment

    def following(self, now: datetime) -> datetime:
        """Следующий срок после `now`, во времени расписания (переход на летнее время учтен)."""
        day = self.slot(now).astimezone(self.zone).date() + timedelta(days=7)
        return _at(day, self.at, self.zone)

    def describe(self) -> str:
        return f"{DAY_NAMES[self.weekday]}, {self.at.strftime('%H:%M')} ({self.zone.key})"


def _at(day: date, at: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=zone)


def parse_day(value: str) -> int | None:
    """День недели: «пн», «понедельник», 1..7. None — напоминание выключено."""
    text = fold(value)
    if text in OFF:
        return None
    if text.isdigit() and 1 <= int(text) <= 7:
        return int(text) - 1
    if text in DAYS:
        return DAYS[text]
    raise ValueError(
        f"BACKUP_REMINDER_DAY: «{value}» не день недели (пн, вт, ср, чт, пт, сб, вс или нет)"
    )


def parse_time(value: str) -> time:
    text = value.strip()
    try:
        hours, minutes = text.split(":")
        return time(int(hours), int(minutes))
    except ValueError as error:
        raise ValueError(f"BACKUP_REMINDER_TIME: «{value}» не время ЧЧ:ММ") from error
