"""Необязательные поля сообщения: чтение, список, старые ожидания и ответы."""

from doc_registry_bot.bot import registration
from doc_registry_bot.registry import markup, message, schema, text

ORGS = ["АБВГ"]
SENDER = "Иванов Иван Иванович"
BASE = "Действие: Регистрация\nНомер: АБВГ.301121.ХХХ\nНаименование: Втулка\nПроект: Станок"
OPTIONAL = (
    "Отметка учета: Учтен\n"
    "Примечание (первичное применение): Изделие\n"
    "Примечание (краткая характеристика): Сталь & покрытие"
)


def test_message_keeps_all_three_optional_fields() -> None:
    request, problems = message.read(f"{BASE}\n{OPTIONAL}", ORGS, SENDER)
    assert problems == []
    assert request is not None
    assert request.accounting == "Учтен"
    assert request.first_use == "Изделие"
    assert request.summary == "Сталь & покрытие"


def test_ready_values_read_the_same_optional_columns() -> None:
    request, problems = message.read_values(text.lines(f"{BASE}\n{OPTIONAL}"), ORGS, SENDER)
    assert problems == []
    assert request is not None
    assert (request.accounting, request.first_use, request.summary) == (
        "Учтен",
        "Изделие",
        "Сталь & покрытие",
    )


def test_optional_fields_are_shared_by_every_number_in_a_batch() -> None:
    body = BASE.replace("Номер: АБВГ.301121.ХХХ", "Номер: АБВГ.301121.ХХХ, АБВГ.806121.ХХХ")
    batch = message.read_batch(f"{body}\n{OPTIONAL}", ORGS, SENDER)
    assert batch.ok
    assert len(batch.items) == 2
    for item in batch.items:
        assert item.request is not None
        assert (item.request.accounting, item.request.first_use, item.request.summary) == (
            "Учтен",
            "Изделие",
            "Сталь & покрытие",
        )


def test_optional_fields_may_be_missing_or_explicitly_empty() -> None:
    empty = (
        "Отметка учета: \t\n"
        "Примечание (первичное применение):\n"
        "Примечание (краткая характеристика):   "
    )
    for suffix in ("", f"\n{empty}"):
        request, problems = message.read(BASE + suffix, ORGS, SENDER)
        assert problems == []
        assert request is not None
        assert (request.accounting, request.first_use, request.summary) == ("", "", "")
        batch = message.read_batch(BASE + suffix, ORGS, SENDER)
        assert batch.ok
        [item] = batch.items
        assert item.request is not None
        assert (item.request.accounting, item.request.first_use, item.request.summary) == (
            "",
            "",
            "",
        )


def test_optional_keys_use_complete_names_without_case_sensitivity() -> None:
    body = f"{BASE}\n{OPTIONAL.lower()}"
    request, problems = message.read(body, ORGS, SENDER)
    assert problems == []
    assert request is not None
    assert (request.accounting, request.first_use, request.summary) == (
        "учтен",
        "изделие",
        "сталь & покрытие",
    )
    abbreviated, problems = message.read(
        f"{BASE}\nОтметка: Учтен\nПервичное применение: Изделие\nХарактеристика: Сталь",
        ORGS,
        SENDER,
    )
    assert problems == []
    assert abbreviated is not None
    assert (abbreviated.accounting, abbreviated.first_use, abbreviated.summary) == ("", "", "")


def test_old_request_payload_uses_empty_optional_values() -> None:
    saved = {
        "org": "АБВГ",
        "eskd": "301121",
        "number": None,
        "name": "Втулка",
        "project": "Станок",
        "fio": SENDER,
    }
    request = message.RegistrationRequest(**saved)
    assert (request.accounting, request.first_use, request.summary) == ("", "", "")


def test_filled_optional_values_are_shown_in_single_and_batch_cards() -> None:
    request, _ = message.read(f"{BASE}\n{OPTIONAL}", ORGS, SENDER)
    assert request is not None
    card = registration.registered_card("АБВГ.301121.001", request, "Зарегистрировано")
    row = registration.PlannedRow(
        1, "АБВГ.301121.001", "Втулка", "Станок", SENDER, "Учтен", "Изделие", "Сталь & покрытие"
    )
    batch_card = registration._batch_card([row], "Зарегистрировано: 1 из 1.")
    for html in (card, batch_card):
        assert "Отметка учета: Учтен" in html
        assert "Примечание первичное: Изделие" in html
        assert "Примечание краткая характеристика: Сталь &amp; покрытие" in html


def test_empty_optional_values_do_not_add_lines_to_previous_cards() -> None:
    request, _ = message.read(BASE, ORGS, SENDER)
    assert request is not None
    card = registration.registered_card("АБВГ.301121.001", request, "Зарегистрировано")
    assert markup.plain(card).splitlines() == [
        "Зарегистрировано",
        "Номер: АБВГ.301121.001",
        "Наименование: Втулка",
        "Проект: Станок",
        f"ФИО: {SENDER}",
        "Подразделение регистрирующего: не указано",
        "Актуальность: Зарегистрирован",
    ]
    row = registration.PlannedRow(1, "АБВГ.301121.001", "Втулка", "Станок", SENDER)
    batch_card = registration._batch_card([row], "Зарегистрировано: 1 из 1.")
    assert markup.plain(batch_card).splitlines() == [
        "Зарегистрировано: 1 из 1.",
        "1. АБВГ.301121.001",
        "Наименование: Втулка",
        "Проект: Станок",
        f"ФИО: {SENDER}",
        "Подразделение регистрирующего: не указано",
        "Актуальность: Зарегистрирован",
    ]


def test_optional_column_names_are_recognized_in_messages() -> None:
    assert message.known_lines(OPTIONAL) == 3
    assert message.known_lines(schema.ACCOUNTING + ":") == 1
