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
    assert "Выгрузить журнал" not in registrar
    assert "Выгрузить журнал" in admin and "Назначить администратора" not in admin
    assert "Назначить администратора" in owner


async def test_the_admin_instruction_is_for_admins_only(stand: Stand) -> None:
    admin = markup.plain(await stand.say(ADMIN, "Инструкция администратора"))
    assert admin.startswith("Инструкция администратора бота регистрации КД")
    assert "Выдать доступ: Иванов Иван Иванович, 123456" in admin
    assert markup.plain(await stand.say(REGISTRAR, "Инструкция администратора")).startswith(
        "Нет прав"
    )
    assert "Инструкция по работе" in await stand.say(REGISTRAR, "Инструкция")


async def test_an_admin_command_from_a_registrar_is_refused(stand: Stand) -> None:
    answer = markup.plain(await stand.say(REGISTRAR, "Выдать доступ: 9999"))
    assert answer.startswith("Нет прав на эту команду")
    assert (await stand.member(STRANGER)) is None


async def test_the_owner_needs_no_access_row(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    done = await stand.say(OWNER, registration())
    assert "<b>АБВГ.301121.002</b>" in done


# ── Люди: только полное ФИО или ID ───────────────────────────────────────────


async def test_a_partial_name_is_refused(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Выдать доступ: Чужов"))
    assert answer == "Чужов: укажите полное ФИО или ID"
    assert (await stand.member(STRANGER)) is None


async def test_the_full_name_finds_one_person(stand: Stand) -> None:
    preview = markup.plain(await stand.say(ADMIN, "Выдать доступ: чужов  степан степанович"))
    assert "Чужов Степан Степанович, ID 9999" in preview


async def test_two_people_with_one_name_require_an_id(stand: Stand) -> None:
    stand.messenger.person(3001, "Петров", "Петр", "Петрович", "Цех 1")
    stand.messenger.person(3002, "Петров", "Петр", "Петрович", "Цех 2")
    answer = markup.plain(await stand.say(ADMIN, "Выдать доступ: Петров Петр Петрович"))
    assert answer == (
        "Петров Петр Петрович: найдено несколько человек, укажите ID: "
        "ID 3001 (Цех 1); ID 3002 (Цех 2)"
    )
    preview = markup.plain(await stand.say(ADMIN, "Выдать доступ: 3002"))
    assert "ID 3002 (Цех 2)" in preview


async def test_an_unknown_person_is_named(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Выдать доступ: Сидоров Сидор Сидорович, 777"))
    assert answer == (
        "Сидоров Сидор Сидорович: не найден среди контактов бота, укажите ID\n"
        "ID 777: не найден в мессенджере"
    )


# ── Доступ ───────────────────────────────────────────────────────────────────


async def test_access_is_granted_and_the_instruction_delivery_is_reported(stand: Stand) -> None:
    preview = markup.plain(await stand.say(ADMIN, "Выдать доступ: 9999"))
    assert "Выдать доступ к регистрации КД:" in preview
    assert "Чужов Степан Степанович, ID 9999" in preview
    done = markup.plain(await stand.say(ADMIN, "Да"))
    assert "Доступ выдан: 1" in done
    assert "Инструкция отправлена: 1 из 1" in done
    member = await stand.member(STRANGER)
    assert member is not None and member.has_access
    [greeting] = [s for s in stand.messenger.sent if s.user_id == STRANGER]
    assert "Инструкция по регистрации документов" in greeting.html
    [audit] = [a for a in await stand.audit() if a.action == "Выдача доступа"]
    assert audit.actor_fio == "Админов Андрей Андреевич"
    assert audit.actor_id == ADMIN


async def test_an_undelivered_instruction_is_named(stand: Stand) -> None:
    stand.messenger.broken.add(STRANGER)
    await stand.say(ADMIN, "Выдать доступ: 9999")
    done = markup.plain(await stand.say(ADMIN, "Да"))
    assert "Инструкция отправлена: 0 из 1" in done
    assert "Не дошла:" in done
    assert "Чужов Степан Степанович, ID 9999" in done
    member = await stand.member(STRANGER)
    assert member is not None and member.has_access


async def test_access_without_instruction_for_the_move(stand: Stand) -> None:
    await stand.say(ADMIN, "Выдать доступ: 9999")
    done = markup.plain(await stand.say(ADMIN, "Без инструкции"))
    assert "Инструкция не отправлялась." in done
    assert stand.messenger.sent == []
    member = await stand.member(STRANGER)
    assert member is not None and member.has_access


async def test_no_cancels_a_grant(stand: Stand) -> None:
    await stand.say(ADMIN, "Выдать доступ: 9999")
    assert markup.plain(await stand.say(ADMIN, "Нет")) == "Операция отменена"
    assert (await stand.member(STRANGER)) is None


async def test_access_is_deleted_after_yes(stand: Stand) -> None:
    preview = markup.plain(await stand.say(ADMIN, "Удалить доступ: Иванов Иван Иванович"))
    assert "Удалить доступ к регистрации КД:" in preview
    assert "Подтвердите: Да или Нет" in preview
    registered = await stand.say(REGISTRAR, "Инструкция")
    assert "Инструкция по работе" in registered
    done = markup.plain(await stand.say(ADMIN, "Да"))
    assert "Доступ удален: 1" in done
    assert "нет доступа" in markup.plain(await stand.say(REGISTRAR, registration()))


async def test_the_instruction_is_sent_again_to_people_with_access(stand: Stand) -> None:
    refused = markup.plain(await stand.say(ADMIN, "Отправить инструкцию: 9999"))
    assert "нет доступа к боту, сначала Выдать доступ" in refused
    preview = markup.plain(await stand.say(ADMIN, "Отправить инструкцию: 1001"))
    assert "Отправить инструкцию:" in preview
    done = markup.plain(await stand.say(ADMIN, "Да"))
    assert done == "Инструкция отправлена: 1 из 1"
    [sent] = stand.messenger.sent
    assert sent.user_id == REGISTRAR


async def test_users_lists_everyone_with_access(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Пользователи"))
    assert "Иванов Иван Иванович, ID 1001" in answer
    assert "Админов Андрей Андреевич, ID 2, администратор" in answer
    assert "ID 1, владелец" in answer


# ── Администраторы ───────────────────────────────────────────────────────────


async def test_only_the_owner_appoints_admins_after_yes(stand: Stand) -> None:
    refused = markup.plain(await stand.say(ADMIN, "Назначить администратора: 1001"))
    assert refused.startswith("Нет прав")
    preview = markup.plain(await stand.say(OWNER, "Назначить администратора: 1001"))
    assert "Назначить администраторами:" in preview
    done = markup.plain(await stand.say(OWNER, "Да"))
    assert "Назначены администраторами: 1" in done
    member = await stand.member(REGISTRAR)
    assert member is not None and member.is_admin
    assert any(
        "Инструкция администратора" in r.html for r in stand.replies if r.user_id == REGISTRAR
    )
    listing = markup.plain(await stand.say(OWNER, "Администраторы"))
    assert "Иванов Иван Иванович, ID 1001" in listing


async def test_an_admin_is_removed_after_yes(stand: Stand) -> None:
    await stand.say(OWNER, "Снять администратора: 2")
    done = markup.plain(await stand.say(OWNER, "Да"))
    assert "Сняты с администраторов: 1" in done
    member = await stand.member(ADMIN)
    assert member is not None and not member.is_admin


async def test_the_owner_cannot_be_removed(stand: Stand) -> None:
    answer = markup.plain(await stand.say(OWNER, "Снять администратора: 1"))
    assert "владельца снять нельзя" in answer


# ── Выгрузки ─────────────────────────────────────────────────────────────────


async def test_export_sends_the_whole_journal_as_csv(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    answer = await stand.say(ADMIN, "Выгрузить журнал")
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
    await stand.say(ADMIN, "Выгрузить журнал")
    answer = markup.plain(await stand.say(ADMIN, "Журнал действий"))
    assert "Выгрузка журнала" in answer
    [reply] = stand.replies
    assert reply.files[0].name.startswith("Журнал действий")


# ── Отправка сообщения ───────────────────────────────────────────────────────


async def test_a_message_is_previewed_then_sent_with_files(stand: Stand) -> None:
    stand.messenger.files[(stand.message_id + 1, 4)] = Attachment("Приказ.pdf", b"%PDF")
    preview = markup.plain(
        await stand.say(
            ADMIN,
            "Отправить сообщение: Иванов Иван Иванович\nСообщение: Собрание в 10:00 <важно>\n"
            "Повестка во вложении.",
            [4],
        )
    )
    assert "Отправить сообщение: получателей 1" in preview
    assert "Собрание в 10:00 <важно>\nПовестка во вложении." in preview
    assert "Файлов: 1" in preview
    assert stand.messenger.sent == []
    assert markup.plain(await stand.say(ADMIN, "Да")) == "Отправлено: 1 из 1"
    [sent] = stand.messenger.sent
    assert sent.user_id == REGISTRAR
    assert sent.html == "Собрание в 10:00 &lt;важно&gt;\nПовестка во вложении."
    assert sent.files[0].name == "Приказ.pdf"


async def test_a_message_without_text_is_refused(stand: Stand) -> None:
    answer = markup.plain(await stand.say(ADMIN, "Отправить сообщение: 1001\nСобрание"))
    assert answer.startswith("Сообщение: не указан текст")


async def test_a_message_to_everyone_with_access(stand: Stand) -> None:
    preview = markup.plain(await stand.say(ADMIN, "Отправить сообщение: всем\nСообщение: Тест"))
    assert "получателей 2" in preview
    assert markup.plain(await stand.say(ADMIN, "Да")) == "Отправлено: 2 из 2"
    assert sorted(s.user_id for s in stand.messenger.sent) == [OWNER, REGISTRAR]


async def test_a_failed_delivery_is_reported(stand: Stand) -> None:
    stand.messenger.broken.add(REGISTRAR)
    await stand.say(ADMIN, "Отправить сообщение: 1001\nСообщение: Текст")
    answer = markup.plain(await stand.say(ADMIN, "Да"))
    assert answer.startswith("Отправлено: 0 из 1")
    assert "Не отправлено:" in answer
