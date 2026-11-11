"""Регистрация по сообщению целиком: сообщение → номер → ответ. Поведение повторяет SmartBase
(RegisterMessageActionTests, RegistrationConfirmationTests, BatchRegistrationTests,
DocumentMessageTests, ReplyMarkupTests)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.registry import markup
from tests.stand import REGISTRAR, STRANGER, Stand, registration

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    return result


async def test_a_stranger_gets_an_access_refusal_without_writing(stand: Stand) -> None:
    answer = await stand.say(STRANGER, registration())
    assert "нет доступа" in markup.plain(answer)
    assert await stand.designations() == []


async def test_the_first_number_of_a_series_asks_and_yes_registers(stand: Stand) -> None:
    preview = await stand.say(REGISTRAR, registration())
    assert preview.startswith(
        "<b>Заявка на регистрацию</b>\nНомер: <b>АБВГ.301121.001</b>\nНаименование: Кронштейн\n"
    )
    assert (
        "\n\n<b>Это первый номер в данной серии классификатора. Вы уверены, что номер верный?</b>"
        "\n\n" in preview
    )
    assert preview.endswith(
        "\n\nПодтвердите: <b>Да</b> или <b>Нет</b>\n<i>Подтверждение действует 10 минут</i>"
    )
    assert await stand.designations() == []
    done = await stand.say(REGISTRAR, "Да")
    assert done == (
        "<b>Зарегистрировано</b>\nНомер: <b>АБВГ.301121.001</b>\nНаименование: Кронштейн\n"
        "Проект: Машина\nФИО: Иванов Иван Иванович"
    )
    assert await stand.designations() == ["АБВГ.301121.001"]
    [entry] = await stand.entries()
    assert entry.fio == "Иванов Иван Иванович"
    assert entry.registrant_id == REGISTRAR


async def test_an_existing_series_registers_at_once_with_the_first_free_number(
    stand: Stand,
) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.003")
    done = await stand.say(REGISTRAR, registration())
    assert done.startswith("<b>Зарегистрировано</b>\nНомер: <b>АБВГ.301121.002</b>")
    assert "АБВГ.301121.002" in await stand.designations()


async def test_an_execution_mask_gives_the_first_free_execution(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.001-01")
    done = await stand.say(REGISTRAR, registration("АБВГ.301121.001-ХХ"))
    assert "<b>АБВГ.301121.001-02</b>" in done


async def test_an_unknown_code_warning_is_bold_as_a_whole(stand: Stand) -> None:
    preview = await stand.say(REGISTRAR, registration("АБВГ.000000.ХХХ"))
    assert (
        "\n\n<b>Код классификации не найден в доступном справочнике ЕСКД. Возможно, он относится "
        "к закрытому классификатору. Вы уверены, что номер указан верно?</b>\n\n" in preview
    )


async def test_the_catalog_chain_is_shown_for_a_new_series(stand: Stand) -> None:
    preview = await stand.say(REGISTRAR, registration("АБВГ.330008.ХХХ"))
    assert "\n33: " in preview
    assert "\n330008: " in preview


async def test_no_cancels_and_nothing_is_written(stand: Stand) -> None:
    await stand.say(REGISTRAR, registration())
    assert await stand.say(REGISTRAR, "нет") == "<b>Операция отменена</b>"
    assert await stand.designations() == []
    assert "нет операции" in await stand.say(REGISTRAR, "Да")


async def test_a_confirmation_expires_after_ten_minutes(stand: Stand) -> None:
    await stand.say(REGISTRAR, registration())
    stand.later(11)
    assert "срок истек" in await stand.say(REGISTRAR, "Да")
    assert await stand.designations() == []


async def test_a_changed_series_refuses_the_confirmation(stand: Stand) -> None:
    await stand.say(REGISTRAR, registration())
    await stand.add("АБВГ.301121.001")
    answer = await stand.say(REGISTRAR, "Да")
    assert answer.startswith("<b>Серия изменилась после запроса подтверждения.</b>")
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_a_ready_number_is_not_accepted_for_registration(stand: Stand) -> None:
    answer = await stand.say(REGISTRAR, registration("АБВГ.301121.001"))
    assert answer == (
        "<b>Номер:</b> неправильный номер (Пример: <i>АААА.000000.ХХХ или АААА.000000.001-ХХ</i>)"
    )


async def test_problems_come_all_at_once_formatted(stand: Stand) -> None:
    answer = await stand.say(REGISTRAR, registration("ЖЗИК.301121.ХХХ", project=""))
    assert answer == (
        "<b>Номер:</b> неизвестный код организации ЖЗИК (Можно: <i>АБВГ, ГМБВ</i>)\n"
        "<b>Проект:</b> не указан проект"
    )


async def test_values_are_escaped_and_reach_the_journal_as_written(stand: Stand) -> None:
    await stand.add("АБВГ.301121.005")
    name = "Кронштейн <левый> & правый"
    done = await stand.say(REGISTRAR, registration(name=name))
    assert "Наименование: Кронштейн &lt;левый&gt; &amp; правый\n" in done
    assert f"Наименование: {name}\n" in markup.plain(done)
    assert any(e.name == name for e in await stand.entries())


async def test_a_repeated_delivery_is_processed_once(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    await stand.say(REGISTRAR, registration(), message_id=555)
    assert await stand.say(REGISTRAR, registration(), message_id=555) == ""
    assert await stand.designations() == ["АБВГ.301121.001", "АБВГ.301121.002"]


async def test_an_incomplete_card_name_blocks_registration(stand: Stand) -> None:
    stand.messenger.person(REGISTRAR, "Иванов", "Иван", "")
    answer = await stand.say(REGISTRAR, registration())
    assert "нет полного ФИО" in markup.plain(answer)
    assert await stand.designations() == []


async def test_a_waiting_confirmation_blocks_a_new_request_but_not_viewing(stand: Stand) -> None:
    await stand.add("АБВГ.806121.001")
    await stand.say(REGISTRAR, registration())
    blocked = await stand.say(REGISTRAR, registration("АБВГ.806121.ХХХ"))
    assert blocked == (
        "<b>Ожидается подтверждение.</b> Для нового запроса сначала ответьте <b>Нет</b>"
    )
    viewed = await stand.say(REGISTRAR, "Действие: Просмотр\nНомер: АБВГ.806121.001")
    assert viewed.startswith("Номер: <b>АБВГ.806121.001</b>")
    assert "Инструкция по работе" in await stand.say(REGISTRAR, "Инструкция")


async def test_an_unknown_action_lists_the_allowed_ones(stand: Stand) -> None:
    answer = await stand.say(REGISTRAR, "Действие: Печать\nНомер: АБВГ.301121.001")
    assert markup.plain(answer) == (
        "Действие: неизвестное действие (Можно: Регистрация, Просмотр, Удаление, Изменение)"
    )


# ── Список ───────────────────────────────────────────────────────────────────


async def test_a_list_in_existing_series_registers_at_once(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.806121.001")
    done = await stand.say(
        REGISTRAR, registration("АБВГ.301121.ХХХ, АБВГ.806121.ХХХ, АБВГ.301121.ХХХ")
    )
    assert done.startswith("<b>Зарегистрировано: 3 из 3.</b>\n1. <b>АБВГ.301121.002</b>\n")
    assert "2. <b>АБВГ.806121.002</b>\n3. <b>АБВГ.301121.003</b>" in done
    assert len(await stand.designations()) == 5


async def test_a_list_with_a_new_series_asks_once_for_everything(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    preview = await stand.say(REGISTRAR, registration("АБВГ.301121.ХХХ, ГМБВ.806121.ХХХ"))
    assert preview.startswith("<b>Заявка на регистрацию: 2</b>")
    assert "<b>Серия ГМБВ.806121:</b>" in preview
    assert "Подтверждение относится ко всему списку" in preview
    assert len(await stand.designations()) == 1
    done = await stand.say(REGISTRAR, "Да")
    assert done.startswith("<b>Зарегистрировано: 2 из 2.</b>")
    assert len(await stand.designations()) == 3


async def test_one_bad_number_rejects_the_whole_list(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    answer = await stand.say(REGISTRAR, registration("АБВГ.301121.ХХХ, АБВГ.30112.ХХХ"))
    plain = markup.plain(answer)
    assert plain.startswith("Заявка отклонена. Зарегистрировано: 0 из 2.")
    assert "2. АБВГ.30112.ХХХ: Номер: неправильный номер" in plain
    assert "Исправьте ошибки и отправьте весь список заново." in plain
    assert await stand.designations() == ["АБВГ.301121.001"]


# ── Просмотр, удаление, изменение ─────────────────────────────────────────────


async def test_view_shows_the_whole_card(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    card = await stand.say(REGISTRAR, "Действие: Просмотр\nНомер: абвг.301121.001")
    assert card == (
        "Номер: <b>АБВГ.301121.001</b>\nНаименование: Кронштейн\nПроект: Машина\n"
        "ФИО: Иванов Иван Иванович\nДата регистрации: 10.11.2026 12:00\nОтметка учета: не указано\n"
        "Примечание (первичное применение): не указано\n"
        "Примечание (краткая характеристика): не указано"
    )


async def test_a_missing_document_is_named(stand: Stand) -> None:
    answer = await stand.say(REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001")
    assert answer == "<b>Номер:</b> документ <b>АБВГ.301121.001</b> не найден"


async def test_delete_asks_then_frees_the_number(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    preview = await stand.say(REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001")
    assert preview.startswith("<b>Подтвердите удаление документа:</b>\nНомер: <b>АБВГ.301121.001")
    done = await stand.say(REGISTRAR, "Да")
    assert done.startswith("<b>Удалено</b>\nНомер: <b>АБВГ.301121.001</b>")
    assert await stand.designations() == ["АБВГ.301121.002"]
    again = await stand.say(REGISTRAR, registration())
    assert "<b>АБВГ.301121.001</b>" in again
    [audit] = [a for a in await stand.audit() if a.action == "Удаление записи"]
    assert "АБВГ.301121.001" in audit.details


async def test_edit_shows_old_and_new_and_saves_on_yes(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    preview = await stand.say(
        REGISTRAR, "Действие: Изменение\nНомер: АБВГ.301121.001\nПроект: Станок"
    )
    assert "<b>Изменения:</b>\nПроект: <s>Машина</s> → <b>Станок</b>" in preview
    done = await stand.say(REGISTRAR, "да")
    assert done.startswith(
        "<b>Изменено</b>\nНомер: <b>АБВГ.301121.001</b>\nНаименование: Кронштейн\n"
        "Проект: <b>Станок</b>\n"
    )
    [entry] = await stand.entries()
    assert entry.project == "Станок"
    assert entry.version == 2


async def test_edit_refuses_the_name_of_the_registrant_and_unknown_fields(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    answer = await stand.say(
        REGISTRAR,
        "Действие: Изменение\nНомер: АБВГ.301121.001\nФИО: Петров Петр Петрович\nЦвет: синий",
    )
    plain = markup.plain(answer)
    assert "ФИО: регистрирующий определяется по карточке отправителя" in plain
    assert "Цвет: изменение этого поля не поддерживается" in plain


async def test_a_document_changed_after_the_preview_is_not_deleted(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    await stand.say(REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001")
    async with stand.db() as session, session.begin():
        from doc_registry_bot.db import journal

        entry = await journal.find(session, "АБВГ.301121.001")
        assert entry is not None
        entry.project = "Другой"
        entry.version += 1
    answer = await stand.say(REGISTRAR, "Да")
    assert answer.startswith("<b>Документ изменился после запроса подтверждения.</b>")
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_several_numbers_are_deleted_with_one_yes(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002", "АБВГ.301121.003")
    preview = await stand.say(
        REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001, абвг.301121.003, АБВГ.301121.001"
    )
    assert preview == (
        "<b>Подтвердите удаление документов: 2</b>\n"
        "1. <b>АБВГ.301121.001</b>: Кронштейн, проект Машина\n"
        "2. <b>АБВГ.301121.003</b>: Кронштейн, проект Машина\n\n" + markup.PROMPT
    )
    assert await stand.designations() == [
        "АБВГ.301121.001",
        "АБВГ.301121.002",
        "АБВГ.301121.003",
    ]
    done = await stand.say(REGISTRAR, "Да")
    assert done.startswith("<b>Удалено: 2</b>\n1. <b>АБВГ.301121.001</b>")
    assert await stand.designations() == ["АБВГ.301121.002"]
    deleted = [a for a in await stand.audit() if a.action == "Удаление записи"]
    assert len(deleted) == 2


async def test_nothing_is_deleted_when_one_number_is_missing(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    answer = await stand.say(
        REGISTRAR,
        "Действие: Удаление\nНомер: АБВГ.301121.001, АБВГ.301121.007, АБВГ.301121.009",
    )
    assert markup.plain(answer) == (
        "Номер: документ АБВГ.301121.007 не найден\n"
        "Номер: документ АБВГ.301121.009 не найден\n"
        "Ничего не удалено"
    )
    assert await stand.designations() == ["АБВГ.301121.001", "АБВГ.301121.002"]
    assert "Регистрация" not in await stand.say(REGISTRAR, "Да")


async def test_a_wrong_number_in_the_list_refuses_the_whole_deletion(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001")
    answer = await stand.say(REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001, АБВГ, ")
    plain = markup.plain(answer)
    assert "Номер: АБВГ: нужен полный номер документа или исполнения" in plain
    assert "Номер: в списке пустой номер" in plain
    assert await stand.designations() == ["АБВГ.301121.001"]


async def test_one_changed_document_stops_the_whole_deletion(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    await stand.say(REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001, АБВГ.301121.002")
    async with stand.db() as session, session.begin():
        from doc_registry_bot.db import journal

        entry = await journal.find(session, "АБВГ.301121.002")
        assert entry is not None
        entry.name = "Другой"
        entry.version += 1
    answer = await stand.say(REGISTRAR, "Да")
    assert answer.startswith("<b>Документы изменились после запроса подтверждения.</b>")
    assert await stand.designations() == ["АБВГ.301121.001", "АБВГ.301121.002"]


async def test_a_new_request_waits_for_the_answer_on_several_deletions(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    await stand.say(REGISTRAR, "Действие: Удаление\nНомер: АБВГ.301121.001, АБВГ.301121.002")
    answer = await stand.say(REGISTRAR, registration())
    assert "Ожидается подтверждение: Удаление, документов: 2" in markup.plain(answer)
    assert await stand.say(REGISTRAR, "Нет")
    assert len(await stand.designations()) == 2


async def test_view_and_edit_take_one_number(stand: Stand) -> None:
    await stand.add("АБВГ.301121.001", "АБВГ.301121.002")
    view = await stand.say(REGISTRAR, "Действие: Просмотр\nНомер: АБВГ.301121.001, АБВГ.301121.002")
    assert markup.plain(view) == "Номер: для действия «Просмотр» указывается один номер"
    edit = await stand.say(
        REGISTRAR, "Действие: Изменение\nНомер: АБВГ.301121.001, АБВГ.301121.002\nПроект: Х"
    )
    assert markup.plain(edit) == "Номер: для действия «Изменение» указывается один номер"


async def test_view_accepts_only_action_and_number(stand: Stand) -> None:
    answer = await stand.say(REGISTRAR, "Действие: Просмотр\nНомер: АБВГ.301121.001\nПроект: Х")
    assert "Проект: для действия «Просмотр» нужны только Действие и Номер" in markup.plain(answer)
