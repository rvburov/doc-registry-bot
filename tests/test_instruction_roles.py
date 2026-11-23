"""Инструкции доступны по текущей роли, включая прямой запрос во время ожидания."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.db.models import Member
from doc_registry_bot.registry import instructions, markup
from tests.stand import ADMIN, OWNER, REGISTRAR, STRANGER, Stand
from tests.texts import assert_human_text

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def stand(db: async_sessionmaker[AsyncSession], config: BotConfig) -> Stand:
    result = Stand(db, config)
    await result.grant(REGISTRAR)
    await result.grant(ADMIN, admin=True)
    await result.add("АБВГ.301121.001")
    return result


@pytest.mark.parametrize("sender", [STRANGER, REGISTRAR, ADMIN, OWNER])
@pytest.mark.parametrize("waiting", [False, True])
@pytest.mark.parametrize(
    "command,required",
    [
        ("Инструкция", 1),
        ("Инструкция администратора", 2),
        ("Инструкция главного администратора", 3),
    ],
)
async def test_direct_requests_obey_roles_even_while_waiting(
    stand: Stand, sender: int, waiting: bool, command: str, required: int
) -> None:
    level = {STRANGER: 0, REGISTRAR: 1, ADMIN: 2, OWNER: 3}[sender]
    if waiting and level:
        assert "Подтвердите" in await stand.say(
            sender, "Действие: Удаление\nНомер: АБВГ.301121.001"
        )
    reply = await stand.say(sender, command)
    if level >= required:
        expected = {1: instructions.TEXT, 2: instructions.ADMIN_TEXT, 3: instructions.OWNER_TEXT}[
            required
        ]
        assert reply == expected
    else:
        assert "Инструкция по работе" not in reply
        assert "Инструкция администратора бота" not in reply
        assert "Инструкция главного администратора бота" not in reply
        assert "Нет прав" in reply or "нет доступа" in reply
    assert await stand.designations() == ["АБВГ.301121.001"]


@pytest.mark.parametrize("sender", [REGISTRAR, ADMIN, OWNER])
async def test_help_only_advertises_accessible_instructions(stand: Stand, sender: int) -> None:
    answer = markup.plain(await stand.say(sender, "Помощь"))
    assert "Инструкция: " in answer
    assert ("Инструкция администратора:" in answer) == (sender in (ADMIN, OWNER))
    assert ("Инструкция главного администратора:" in answer) == (sender == OWNER)


async def test_removing_admin_rights_also_removes_instruction_access(stand: Stand) -> None:
    assert await stand.say(ADMIN, "Инструкция администратора") == instructions.ADMIN_TEXT
    async with stand.db() as session, session.begin():
        member = await session.get(Member, ADMIN)
        assert member is not None
        member.is_admin = False
    assert "Нет прав" in await stand.say(ADMIN, "Инструкция администратора")


async def test_guides_keep_only_their_role_content() -> None:
    basic, admin, owner = map(
        markup.plain, (instructions.TEXT, instructions.ADMIN_TEXT, instructions.OWNER_TEXT)
    )
    for value in (basic, admin, owner):
        assert_human_text(value)
    assert "Регистрация за другого сотрудника" not in basic
    assert "ФИО: Петров Петр Петрович" not in basic
    assert "Актуальность: Аннулирован" not in basic
    assert "1. Работа с документами" in admin
    assert "Регистрация за другого сотрудника" in admin
    assert "Изменение данных существующей записи" in admin
    for command in (
        "Назначить администратора:",
        "Снять администратора:",
        "Резервная копия",
        "Заменить журнал",
    ):
        assert command not in basic and command not in admin
        assert command in owner
