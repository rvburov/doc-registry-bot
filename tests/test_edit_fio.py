"""Администратор исправляет ФИО записи после подтверждения, история хранит исполнителя."""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import JournalEntry, Member
from doc_registry_bot.registry import markup
from tests.stand import ADMIN, OWNER, REGISTRAR, Stand

pytestmark = pytest.mark.asyncio

NUMBER = "АБВГ.301121.001"
ORIGINAL = "Иванов Иван Иванович"
TARGET = "Петров Петр Петрович"
SECOND = "Сидоров Сидор Сидорович"
PRESERVED = (
    "id",
    "designation",
    "designation_key",
    "series",
    "name",
    "registered_at",
    "project",
    "accounting",
    "first_use",
    "summary",
    "registrant_id",
)


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    await result.grant(OWNER)
    await result.add(NUMBER)
    async with result.db() as session, session.begin():
        entry = await journal.find(session, NUMBER)
        assert entry is not None
        entry.registered_at = result.now - timedelta(days=10)
        entry.updated_at = entry.registered_at
        entry.registrant_id = REGISTRAR
        entry.version = 7
        entry.accounting = "Учтен"
        entry.first_use = "Изделие"
        entry.summary = "Сталь"
    return result


def edit(fio: str = TARGET, project: str | None = None) -> str:
    body = f"Действие: Изменение\nНомер: {NUMBER}\nФИО: {fio}"
    if project is not None:
        body += f"\nПроект: {project}"
    return body


def snapshot(entry: JournalEntry) -> tuple[object, ...]:
    return tuple(getattr(entry, field) for field in (*PRESERVED, "fio", "version", "updated_at"))


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_admin_changes_only_fio_after_showing_old_and_new_values(
    stand: Stand, sender: int
) -> None:
    [before] = await stand.entries()
    preview = await stand.say(sender, edit())
    assert "Подтвердите изменение документа:" in preview
    assert f"ФИО: <s>{ORIGINAL}</s> → <b>{TARGET}</b>" in preview
    assert "Подтвердите:" in preview
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []

    done = markup.plain(await stand.say(sender, "Да"))
    assert done.startswith("Изменено\n")
    assert f"ФИО: {TARGET}" in done
    [after] = await stand.entries()
    assert after.fio == TARGET
    assert all(getattr(after, field) == getattr(before, field) for field in PRESERVED)
    assert after.version == before.version + 1
    assert after.updated_at == stand.now

    [event] = await stand.audit()
    assert event.action == "Изменение записи"
    assert event.actor_id == sender
    assert event.actor_fio == stand.messenger.cards[sender].fio
    assert event.at == stand.now
    assert NUMBER in event.details
    assert f"ФИО: {ORIGINAL} → {TARGET}" in event.details

    # Та же квитанция «Да» не применяет изменение повторно и не дублирует историю.
    assert await stand.say(sender, "Да", message_id=stand.message_id) == ""
    assert snapshot((await stand.entries())[0]) == snapshot(after)
    assert len(await stand.audit()) == 1


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_admin_can_change_fio_and_project_in_one_operation(stand: Stand, sender: int) -> None:
    [before] = await stand.entries()
    preview = markup.plain(await stand.say(sender, edit(project="Стенд")))
    assert f"ФИО: {ORIGINAL} → {TARGET}" in preview
    assert "Проект: Машина → Стенд" in preview
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert "Изменено" in await stand.say(sender, "Да")
    [after] = await stand.entries()
    assert after.fio == TARGET
    assert after.project == "Стенд"
    assert after.version == before.version + 1
    [event] = await stand.audit()
    assert f"ФИО: {ORIGINAL} → {TARGET}" in event.details
    assert "Проект: Машина → Стенд" in event.details


@pytest.mark.parametrize("project", [None, "Стенд"])
async def test_regular_user_cannot_change_fio_even_with_another_allowed_field(
    stand: Stand, project: str | None
) -> None:
    [before] = await stand.entries()
    answer = markup.plain(await stand.say(REGISTRAR, edit(project=project)))
    assert "ФИО: регистрирующий определяется по карточке отправителя" in answer
    assert "Подтвердите изменение" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
@pytest.mark.parametrize("fio", ["", "Петров П.П.", "Петров Петр", "Петров"])
async def test_admin_cannot_save_empty_or_incomplete_fio(
    stand: Stand, sender: int, fio: str
) -> None:
    [before] = await stand.entries()
    answer = markup.plain(await stand.say(sender, edit(fio, project="Стенд")))
    assert "ФИО:" in answer
    assert "Подтвердите изменение" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_fio_spaces_are_collapsed_before_preview_and_storage(
    stand: Stand, sender: int
) -> None:
    preview = markup.plain(await stand.say(sender, edit("  Петров   Петр   Петрович  ")))
    assert f"ФИО: {ORIGINAL} → {TARGET}" in preview
    assert "Изменено" in await stand.say(sender, "Да")
    [entry] = await stand.entries()
    assert entry.fio == TARGET
    [event] = await stand.audit()
    assert f"ФИО: {ORIGINAL} → {TARGET}" in event.details


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_unchanged_fio_does_not_create_confirmation_or_audit(
    stand: Stand, sender: int
) -> None:
    [before] = await stand.entries()
    answer = markup.plain(await stand.say(sender, edit(ORIGINAL)))
    assert "новые значения совпадают с текущими" in answer
    assert "Подтвердите изменение" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_canceling_fio_edit_changes_nothing(stand: Stand, sender: int) -> None:
    [before] = await stand.entries()
    assert "Подтвердите изменение" in await stand.say(sender, edit())
    assert "Операция отменена" in await stand.say(sender, "Нет")
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


@pytest.mark.parametrize("project", [None, "Стенд"])
async def test_losing_admin_rights_before_yes_refuses_the_whole_edit(
    stand: Stand, project: str | None
) -> None:
    [before] = await stand.entries()
    assert "Подтвердите изменение" in await stand.say(ADMIN, edit(project=project))
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
        assert member.has_access
    answer = markup.plain(await stand.say(ADMIN, "Да"))
    assert "Изменено" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


@pytest.mark.parametrize("changed_field", ["fio", "project"])
async def test_stale_confirmation_does_not_overwrite_concurrent_changes(
    stand: Stand, changed_field: str
) -> None:
    assert "Подтвердите изменение" in await stand.say(ADMIN, edit())
    async with stand.db() as session, session.begin():
        entry = await journal.find(session, NUMBER)
        assert entry is not None
        setattr(entry, changed_field, SECOND if changed_field == "fio" else "Другой проект")
        entry.version += 1
    [concurrent] = await stand.entries()
    answer = markup.plain(await stand.say(ADMIN, "Да"))
    assert "Документ изменился после запроса подтверждения" in answer
    assert snapshot((await stand.entries())[0]) == snapshot(concurrent)
    assert await stand.audit() == []


async def test_confirmation_does_not_recreate_a_deleted_record(stand: Stand) -> None:
    assert "Подтвердите изменение" in await stand.say(ADMIN, edit())
    async with stand.db() as session, session.begin():
        entry = await journal.find(session, NUMBER)
        assert entry is not None
        await session.delete(entry)
    answer = markup.plain(await stand.say(ADMIN, "Да"))
    assert f"документ {NUMBER} не найден" in answer
    assert await stand.entries() == []
    assert await stand.audit() == []


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
@pytest.mark.parametrize("field", ["Дата регистрации", "Обозначение"])
async def test_other_unsupported_fields_still_refuse_the_entire_edit(
    stand: Stand, sender: int, field: str
) -> None:
    [before] = await stand.entries()
    answer = markup.plain(await stand.say(sender, edit() + f"\n{field}: Новое значение"))
    assert f"{field}: изменение этого поля не поддерживается" in answer
    assert "Подтвердите изменение" not in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []


async def test_regular_user_keeps_the_right_to_edit_project(stand: Stand) -> None:
    body = f"Действие: Изменение\nНомер: {NUMBER}\nПроект: Станок"
    assert "Подтвердите изменение" in await stand.say(REGISTRAR, body)
    assert "Изменено" in await stand.say(REGISTRAR, "Да")
    [entry] = await stand.entries()
    assert entry.project == "Станок"
    assert entry.fio == ORIGINAL
    assert entry.registrant_id == REGISTRAR


async def test_owner_without_a_member_row_is_named_by_their_actual_card_in_audit(
    stand: Stand,
) -> None:
    async with stand.db() as session, session.begin():
        member = await session.get(Member, OWNER)
        assert member is not None
        await session.delete(member)
    assert "Подтвердите изменение" in await stand.say(OWNER, edit())
    assert "Изменено" in await stand.say(OWNER, "Да")
    [entry] = await stand.entries()
    assert entry.fio == TARGET
    [event] = await stand.audit()
    assert event.actor_id == OWNER
    assert event.actor_fio == stand.messenger.cards[OWNER].fio
    assert event.actor_fio != TARGET


async def test_edit_audit_refreshes_actor_fio_from_their_card(stand: Stand) -> None:
    stand.messenger.person(ADMIN, "Новиков", "Николай", "Николаевич")
    assert "Подтвердите изменение" in await stand.say(ADMIN, edit())
    assert "Изменено" in await stand.say(ADMIN, "Да")
    [event] = await stand.audit()
    assert event.actor_fio == "Новиков Николай Николаевич"
    assert (await stand.entries())[0].fio == TARGET


@pytest.mark.parametrize("sender", [ADMIN, OWNER])
async def test_fio_editor_needs_their_own_full_card_name(stand: Stand, sender: int) -> None:
    [before] = await stand.entries()
    stand.messenger.person(sender, "Админов", "Андрей", "")
    answer = markup.plain(await stand.say(sender, edit()))
    assert "нет полного ФИО" in answer
    assert snapshot((await stand.entries())[0]) == snapshot(before)
    assert await stand.audit() == []
