"""Книга вложением и команды администраторов и владельца."""

import csv
import io

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.types import Attachment
from doc_registry_bot.registry import markup
from tests import books
from tests.stand import ADMIN, OWNER, REGISTRAR, STRANGER, Stand, registration

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    return result


def attach(stand: Stand, data: bytes, name: str = "Список.xlsx") -> list[int]:
    stand.messenger.files[(stand.message_id + 1, 7)] = Attachment(name, data)
    return [7]


# ── Книга ────────────────────────────────────────────────────────────────────


async def test_a_book_in_existing_series_registers_every_row(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    data = books.create(books.Row(2, name="Втулка", accounting="Учтен"), books.Row(5, name="Вал"))
    done = await stand.say(REGISTRAR, "", attach(stand, data))
    assert done.startswith("<b>Зарегистрировано: 2 из 2.</b>\nФайл: Список.xlsx\n")
    assert "Строка 2: <b>АБВГ.301121.002</b>\nСтрока 5: <b>АБВГ.301121.003</b>" in done
    entries = await stand.entries()
    assert [e.designation for e in entries][1:] == ["АБВГ.301121.002", "АБВГ.301121.003"]
    assert entries[1].accounting == "Учтен"
    assert all(e.fio == "Иванов Иван Иванович" for e in entries[1:])


async def test_a_book_with_a_new_series_waits_for_yes(stand: Stand) -> None:
    data = books.create(books.Row(2))
    preview = await stand.say(REGISTRAR, "", attach(stand, data))
    assert "Подтверждение относится ко всему файлу" in preview
    assert await stand.designations() == []
    done = await stand.say(REGISTRAR, "Да")
    assert done.startswith("<b>Зарегистрировано: 1 из 1.</b>")
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_one_bad_row_rejects_the_whole_book(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    data = books.create(books.Row(2), books.Row(3, number="АБВГ.301121.014"))
    answer = markup.plain(await stand.say(REGISTRAR, "", attach(stand, data)))
    assert answer.startswith("Заявка отклонена. Зарегистрировано: 0 из 2.")
    assert "Строка 3: АБВГ.301121.014: Номер: неправильный номер" in answer
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_a_wrong_format_is_refused_before_reading(stand: Stand) -> None:
    answer = markup.plain(await stand.say(REGISTRAR, "", attach(stand, b"abc", "Список.xls")))
    assert answer.startswith("Заявка отклонена. Зарегистрировано: 0.")
    assert ".xls" in answer


# ── Помощь и права ───────────────────────────────────────────────────────────


async def test_help_shows_only_own_commands(stand: Stand) -> None:
    registrar = markup.plain(await stand.say(REGISTRAR, "Помощь"))
    admin = markup.plain(await stand.say(ADMIN, "помощь"))
    owner = markup.plain(await stand.say(OWNER, "Помощь"))
    assert "Выгрузка" not in registrar
    assert "Выгрузка" in admin and "Админ ID" not in admin
    assert "Админ ID" in owner


async def test_an_admin_command_from_a_registrar_is_refused(stand: Stand) -> None:
    answer = markup.plain(await stand.say(REGISTRAR, "Доступ 9999"))
    assert answer.startswith("Нет прав на эту команду")
    assert (await stand.member(STRANGER)) is None


async def test_the_owner_needs_no_access_row(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    done = await stand.say(OWNER, registration())
    assert "<b>АБВГ.301121.002</b>" in done


# ── Доступ ───────────────────────────────────────────────────────────────────


async def test_access_is_granted_with_the_instruction_after_yes(stand: Stand) -> None:
    preview = markup.plain(await stand.say(ADMIN, "Доступ 9999"))
    assert "Выдать доступ к регистрации КД:" in preview
    assert "Чужов Степан Степанович, ID 9999" in preview
    done = await stand.say(ADMIN, "Да")
    assert "Доступ выдан: 1" in markup.plain(done)
    member = await stand.member(STRANGER)
    assert member is not None and member.has_access
    greeting = [r for r in stand.replies if r.user_id == STRANGER]
    assert greeting and "Инструкция по регистрации документов" in greeting[0].html
    assert any(a.action == "Выдача доступа" for a in await stand.audit())


async def test_access_without_instruction_for_the_move(stand: Stand) -> None:
    await stand.say(ADMIN, "Доступ 9999")
    done = markup.plain(await stand.say(ADMIN, "Без инструкции"))
    assert "Инструкция не отправлялась." in done
    assert [r for r in stand.replies if r.user_id == STRANGER] == []
    member = await stand.member(STRANGER)
    assert member is not None and member.has_access


async def test_an_ambiguous_name_is_chosen_by_number(stand: Stand) -> None:
    stand.messenger.person(3001, "Петров", "Петр", "Петрович", "Цех 1")
    stand.messenger.person(3002, "Петров", "Павел", "Петрович", "Цех 2")
    asked = markup.plain(await stand.say(ADMIN, "Доступ Петров, Чужов"))
    assert "Найдено несколько человек" in asked
    assert "1. Петров Петр Петрович, ID 3001 (Цех 1)" in asked
    assert "2. Петров Павел Петрович, ID 3002 (Цех 2)" in asked
    assert "Ответьте номерами" in asked
    bad = markup.plain(await stand.say(ADMIN, "5"))
    assert "от 1 до 2" in bad
    preview = markup.plain(await stand.say(ADMIN, "2"))
    assert "Петров Павел Петрович" in preview and "Чужов Степан Степанович" in preview
    assert "Петров Петр Петрович" not in preview
    await stand.say(ADMIN, "Да")
    member = await stand.member(3002)
    assert member is not None and member.has_access


async def test_an_unknown_person_is_named(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Доступ Сидоров"))
    assert answer == "Сидоров: не найден среди контактов бота"


async def test_access_is_revoked(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Забрать доступ 1001"))
    assert "Доступ снят: 1" in answer
    assert "нет доступа" in markup.plain(await stand.say(REGISTRAR, registration()))


async def test_users_lists_everyone_with_access(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Пользователи"))
    assert "Иванов Иван Иванович, ID 1001" in answer
    assert "Админов Андрей Андреевич, ID 2, администратор" in answer
    assert "ID 1, владелец" in answer


# ── Администраторы ───────────────────────────────────────────────────────────


async def test_only_the_owner_appoints_admins(stand: Stand) -> None:
    refused = markup.plain(await stand.say(ADMIN, "Админ 1001"))
    assert refused.startswith("Нет прав")
    done = markup.plain(await stand.say(OWNER, "Админ 1001"))
    assert "Назначены администраторами: 1" in done
    member = await stand.member(REGISTRAR)
    assert member is not None and member.is_admin
    assert any("права администратора" in r.html for r in stand.replies if r.user_id == REGISTRAR)
    listing = markup.plain(await stand.say(OWNER, "Админы"))
    assert "Иванов Иван Иванович, ID 1001" in listing


async def test_the_owner_cannot_be_removed(stand: Stand) -> None:
    answer = markup.plain(await stand.say(OWNER, "Снять админа 1"))
    assert "владельца снять нельзя" in answer


# ── Выгрузки ─────────────────────────────────────────────────────────────────


async def test_export_sends_the_whole_journal_as_csv(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    answer = await stand.say(ADMIN, "Выгрузка")
    assert "Записей: 2" in markup.plain(answer)
    [reply] = stand.replies
    [file] = reply.files
    assert file.name.endswith(".csv")
    assert file.data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(file.data.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:2] == ["Наименование", "Обозначение"]
    assert [row[1] for row in rows[1:]] == ["АБВГ.301121.001", "АБВГ.301121.002"]
    assert rows[1][3] == "10.11.2026 12:00"


async def test_the_action_log_is_sent_as_csv(stand: Stand) -> None:
    await stand.say(ADMIN, "Выгрузка")
    answer = markup.plain(await stand.say(ADMIN, "Журнал действий"))
    assert "Выгрузка журнала" in answer
    [reply] = stand.replies
    assert reply.files[0].name.startswith("Журнал действий")


# ── Отправка ─────────────────────────────────────────────────────────────────


async def test_a_message_to_one_person_goes_at_once_with_files(stand: Stand) -> None:
    stand.messenger.files[(stand.message_id + 1, 4)] = Attachment("Приказ.pdf", b"%PDF")
    answer = markup.plain(await stand.say(ADMIN, "Отправить: 1001\nСобрание в 10:00 <важно>", [4]))
    assert answer == "Отправлено: 1 из 1"
    [sent] = stand.messenger.sent
    assert sent.user_id == REGISTRAR
    assert sent.html == "Собрание в 10:00 &lt;важно&gt;"
    assert sent.files[0].name == "Приказ.pdf"


async def test_a_message_to_several_people_asks_first(stand: Stand) -> None:
    preview = markup.plain(await stand.say(ADMIN, "Отправить всем\nПроверка связи"))
    assert "Отправить сообщение получателям: 2" in preview
    assert stand.messenger.sent == []
    done = markup.plain(await stand.say(ADMIN, "Да"))
    assert done == "Отправлено: 2 из 2"
    assert sorted(s.user_id for s in stand.messenger.sent) == [OWNER, REGISTRAR]


async def test_a_failed_delivery_is_reported(stand: Stand) -> None:
    stand.messenger.broken.add(REGISTRAR)
    answer = markup.plain(await stand.say(ADMIN, "Отправить: 1001\nТекст"))
    assert answer.startswith("Отправлено: 0 из 1")
    assert "Не отправлено:" in answer
