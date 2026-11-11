"""Регистрация: одна заявка, список через запятую, книга Excel.

Номер подбирается под блокировкой серии в той же транзакции, что и запись. Первый документ
новой серии ждет подтверждения: опечатка в коде классификации иначе молча открыла бы новую серию.
При «Да» снимок серии сверяется с текущим: изменилась — регистрация не выполняется.
"""

from dataclasses import asdict, dataclass
from typing import Any

from doc_registry_bot.bot import pending
from doc_registry_bot.bot.context import Ctx
from doc_registry_bot.db import journal
from doc_registry_bot.registry import designation as d
from doc_registry_bot.registry import eskd, markup, message, text, workbook
from doc_registry_bot.registry.markup import bold, escape
from doc_registry_bot.registry.message import BatchRead, RegistrationRequest

REGISTERED = "Зарегистрировано"
EMPTY_NUMBER = "(пустой номер)"


@dataclass(frozen=True)
class PlannedRow:
    """Строка заявки с уже подобранным обозначением."""

    line: int
    designation: str
    name: str
    project: str
    fio: str
    accounting: str = ""
    first_use: str = ""
    summary: str = ""


@dataclass(frozen=True)
class Series:
    """Снимок серии на момент подбора: по нему «Да» узнает, что серия не менялась."""

    org: str
    eskd: str
    values: list[str]


# ── Ответы ────────────────────────────────────────────────────────────────────


def refused(ctx: Ctx, plain: str) -> None:
    """Отказ с ошибками в сообщении: отправитель исправит их сам."""
    ctx.reply(markup.problems(plain))


def refused_headed(ctx: Ctx, plain: str) -> None:
    ctx.reply(markup.headed(plain))


def registered_card(designation: str, request: RegistrationRequest, heading: str) -> str:
    return "\n".join(
        [
            bold(escape(heading)),
            f"{message.NUMBER_KEY}: {bold(escape(designation))}",
            f"{message.NAME_KEY}: {escape(request.name)}",
            f"{message.PROJECT_KEY}: {escape(request.project)}",
            f"{message.FIO_KEY}: {escape(request.fio)}",
        ]
    )


def _chain(code: str) -> str:
    return "\n".join(
        f"{entry.code}: {escape(entry.name or 'наименование отсутствует в справочнике')}"
        for entry in eskd.chain(code)
    )


_UNKNOWN_CODE = (
    "Код классификации не найден в доступном справочнике ЕСКД. Возможно, он относится к "
    "закрытому классификатору. Вы уверены, что номер указан верно?"
)
_FIRST_NUMBER = "Это первый номер в данной серии классификатора. Вы уверены, что номер верный?"


def first_series_warning(series: Series) -> str:
    label = bold(escape(f"Серия {series.org}.{series.eskd}:"))
    if eskd.find(series.eskd) is None:
        return label + "\n" + bold(_UNKNOWN_CODE)
    return label + "\n" + _chain(series.eskd) + "\n" + bold(_FIRST_NUMBER)


def _refuse_batch(ctx: Ctx, count: int, problems: list[str]) -> None:
    heading = f"Заявка отклонена. Зарегистрировано: 0 из {count}."
    ctx.reply(bold(heading) + "\n\n" + markup.problems("\n".join(problems)))


def _refuse_file(ctx: Ctx, count: int, problems: list[str]) -> None:
    if count > 0:
        _refuse_batch(ctx, count, problems)
        return
    # Файл еще не прочитан: число строк неизвестно, «0 из 0» выдало бы догадку за результат.
    heading = "Заявка отклонена. Зарегистрировано: 0."
    ctx.reply(bold(heading) + "\n\n" + markup.problems("\n".join(problems)))


def _batch_card(rows: list[PlannedRow], heading: str) -> str:
    first = rows[0]
    return (
        bold(escape(heading))
        + "\n"
        + "\n".join(f"{row.line}. {bold(escape(row.designation))}" for row in rows)
        + f"\nНаименование: {escape(first.name)}\nПроект: {escape(first.project)}"
        + f"\nФИО: {escape(first.fio)}"
    )


def _file_card(file_name: str, rows: list[PlannedRow], heading: str) -> str:
    return (
        bold(escape(heading))
        + "\nФайл: "
        + escape(file_name)
        + "\nФИО регистрирующего: "
        + escape(rows[0].fio)
        + "\n"
        + "\n".join(f"Строка {row.line}: {bold(escape(row.designation))}" for row in rows)
    )


# ── Подбор номера ─────────────────────────────────────────────────────────────


def allocate(request: RegistrationRequest, occupied: list[str]) -> tuple[str | None, str]:
    """Обозначение для заявки среди занятых; иначе пустое значение и причина."""
    org = text.org(request.org)
    number = request.number or d.first_free(occupied, org, request.eskd)
    if number is None:
        return None, "Номер: в серии заняты все номера 001-999"
    if request.new_execution:
        execution = d.first_free_execution(occupied, org, request.eskd, number)
        if execution is None:
            return None, "Номер: у документа заняты все исполнения 01-99"
        return d.format_execution(request.org, request.eskd, number, execution), ""
    if any(d.number_in(value, org, request.eskd) == number for value in occupied):
        return None, (
            f"Номер: документ {d.format_number(request.org, request.eskd, number)} "
            "уже зарегистрирован"
        )
    return d.format_number(request.org, request.eskd, number), ""


async def _snapshots(ctx: Ctx, pairs: list[tuple[str, str]]) -> list[Series]:
    unique = list(dict.fromkeys(pairs))
    await journal.lock_series(
        ctx.session, [journal.designation_key(d.prefix(org, code)) for org, code in unique]
    )
    return [
        Series(org, code, await journal.series_designations(ctx.session, org, code))
        for org, code in unique
    ]


async def _write(ctx: Ctx, rows: list[PlannedRow]) -> None:
    await journal.add_entries(
        ctx.session,
        [
            journal.NewEntry(
                designation=row.designation,
                name=row.name,
                fio=row.fio,
                project=row.project,
                accounting=row.accounting,
                first_use=row.first_use,
                summary=row.summary,
                registrant_id=ctx.sender_id,
            )
            for row in rows
        ],
        ctx.now,
    )


def _payload(kind_rows: list[PlannedRow], series: list[Series], **extra: Any) -> dict[str, Any]:
    return {
        "rows": [asdict(row) for row in kind_rows],
        "series": [asdict(item) for item in series],
        **extra,
    }


def _rows(payload: dict[str, Any]) -> list[PlannedRow]:
    return [PlannedRow(**row) for row in payload["rows"]]


def _series(payload: dict[str, Any]) -> list[Series]:
    return [Series(**item) for item in payload["series"]]


# ── Одна заявка ───────────────────────────────────────────────────────────────


async def register(ctx: Ctx, request: RegistrationRequest) -> None:
    [series] = await _snapshots(ctx, [(request.org, request.eskd)])
    planned, problem = allocate(request, series.values)
    if planned is None:
        refused(ctx, problem)
        return
    row = PlannedRow(0, planned, request.name, request.project, request.fio)
    if not series.values:
        # Пустая серия — любой первый документ или исполнение: просим подтверждения.
        await pending.put(ctx, "register", _payload([row], [series], request=asdict(request)))
        if eskd.find(request.eskd) is None:
            warning = bold(_UNKNOWN_CODE)
        else:
            warning = _chain(request.eskd) + "\n\n" + bold(_FIRST_NUMBER)
        card = registered_card(planned, request, "Заявка на регистрацию")
        ctx.reply(f"{card}\n\n{warning}\n\n{markup.PROMPT}")
        return
    await _write(ctx, [row])
    ctx.reply(registered_card(planned, request, REGISTERED))


# ── Список ────────────────────────────────────────────────────────────────────


def _status(series: Series) -> str:
    if series.values:
        return "проверен. Серия уже существует."
    if eskd.find(series.eskd) is None:
        return (
            "код классификации не найден в доступном справочнике ЕСКД. "
            "Для новой серии потребуется подтверждение."
        )
    return "проверен. Код ЕСКД найден в справочнике. Для новой серии потребуется подтверждение."


async def register_batch(ctx: Ctx, batch: BatchRead) -> None:
    requests = [item.request for item in batch.items if item.request is not None]
    snapshots = await _snapshots(ctx, [(r.org, r.eskd) for r in requests])
    occupied = {(s.org, s.eskd): list(s.values) for s in snapshots}
    by_series = {(s.org, s.eskd): s for s in snapshots}
    rows: list[PlannedRow] = []
    statuses: list[str] = []
    has_errors = not batch.ok
    for item in batch.items:
        prefix = f"{item.position}. {item.raw or EMPTY_NUMBER}: "
        if item.request is None or item.problems:
            has_errors = True
            statuses.append(prefix + "; ".join(item.problems))
            continue
        request = item.request
        values = occupied[(request.org, request.eskd)]
        planned, problem = allocate(request, values)
        if planned is None:
            has_errors = True
            statuses.append(prefix + problem)
            continue
        # Повтор маски намеренный: следующий номер видит уже подобранное обозначение.
        values.append(journal.designation_key(planned))
        rows.append(PlannedRow(item.position, planned, request.name, request.project, request.fio))
        statuses.append(prefix + _status(by_series[(request.org, request.eskd)]))
    if has_errors:
        _refuse_batch(
            ctx,
            batch.count,
            [*batch.problems, *statuses, "Исправьте ошибки и отправьте весь список заново."],
        )
        return
    new_series = [s for s in snapshots if not s.values]
    if not new_series:
        await _write(ctx, rows)
        ctx.reply(_batch_card(rows, f"Зарегистрировано: {len(rows)} из {len(rows)}."))
        return
    await pending.put(ctx, "batch", _payload(rows, snapshots))
    warnings = "\n\n".join(first_series_warning(s) for s in new_series)
    ctx.reply(
        _batch_card(rows, f"Заявка на регистрацию: {len(rows)}")
        + f"\n\n{warnings}\n\n"
        + bold("Подтверждение относится ко всему списку. До ответа Да ничего не зарегистрировано.")
        + "\n"
        + markup.PROMPT
    )


# ── Книга Excel ───────────────────────────────────────────────────────────────


async def register_file(ctx: Ctx, data: bytes, file_name: str) -> None:
    try:
        name = workbook.safe_name(file_name)
        source = workbook.read_message_book(data, name, ctx.sender_fio)
    except workbook.WorkbookError as error:
        _refuse_file(ctx, 0, [str(error)])
        return
    items = []
    for row in source:
        request, problems = message.read_values(
            [
                (message.ACTION_KEY, message.REGISTER_WORD),
                (message.NUMBER_KEY, row.designation),
                (message.NAME_KEY, row.name),
                (message.PROJECT_KEY, row.project),
                (message.FIO_KEY, row.fio),
            ],
            ctx.orgs,
        )
        items.append((row, request, problems))
    snapshots = await _snapshots(ctx, [(r.org, r.eskd) for _, r, _ in items if r is not None])
    occupied = {(s.org, s.eskd): list(s.values) for s in snapshots}
    rows: list[PlannedRow] = []
    problems_all: list[str] = []
    for row, request, problems in items:
        prefix = f"Строка {row.line}: {row.designation or EMPTY_NUMBER}: "
        if request is None or problems:
            problems_all.append(prefix + "; ".join(problems))
            continue
        values = occupied[(request.org, request.eskd)]
        planned, problem = allocate(request, values)
        if planned is None:
            problems_all.append(prefix + problem)
            continue
        values.append(journal.designation_key(planned))
        rows.append(
            PlannedRow(
                row.line,
                planned,
                request.name,
                request.project,
                request.fio,
                row.accounting,
                row.first_use,
                row.summary,
            )
        )
    if problems_all:
        _refuse_file(
            ctx,
            len(source),
            [f"Файл: {name}", *problems_all, "Исправьте ошибки и отправьте весь файл заново."],
        )
        return
    if all(s.values for s in snapshots):
        await _write(ctx, rows)
        ctx.reply(_file_card(name, rows, f"Зарегистрировано: {len(rows)} из {len(rows)}."))
        return
    await pending.put(ctx, "file", _payload(rows, snapshots, file_name=name))
    warnings = "\n\n".join(first_series_warning(s) for s in snapshots if not s.values)
    ctx.reply(
        _file_card(name, rows, f"Заявка на регистрацию: {len(rows)}")
        + f"\n\n{warnings}\n\n"
        + bold("Подтверждение относится ко всему файлу. До ответа Да ничего не зарегистрировано.")
        + "\n"
        + markup.PROMPT
    )


# ── Подтверждение ─────────────────────────────────────────────────────────────


async def confirm(ctx: Ctx, kind: str, payload: dict[str, Any]) -> None:
    """Ответ «Да» на заявку с новой серией: сверка снимка серий и запись."""
    rows = _rows(payload)
    series = _series(payload)
    count = len(rows)
    allowed = {text.org(code) for code in ctx.orgs}
    if any(text.org(item.org) not in allowed for item in series):
        problem = "Номер: код организации больше не разрешен. Отправьте запрос заново."
        if kind == "register":
            refused(ctx, problem)
        else:
            _refuse_batch(ctx, count, [problem])
        return
    current = await _snapshots(ctx, [(item.org, item.eskd) for item in series])
    if any(old.values != new.values for old, new in zip(series, current, strict=True)):
        if kind == "register":
            refused_headed(
                ctx,
                "Серия изменилась после запроса подтверждения. Регистрация не выполнена. "
                "Отправьте запрос заново",
            )
        else:
            whole = "весь файл" if kind == "file" else "весь список"
            _refuse_batch(
                ctx,
                count,
                [
                    "Серия изменилась после запроса подтверждения. Регистрация не выполнена. "
                    f"Отправьте {whole} заново."
                ],
            )
        return
    await _write(ctx, rows)
    if kind == "register":
        request = RegistrationRequest(**payload["request"])
        ctx.reply(registered_card(rows[0].designation, request, REGISTERED))
    elif kind == "batch":
        ctx.reply(_batch_card(rows, f"Зарегистрировано: {count} из {count}."))
    else:
        ctx.reply(_file_card(payload["file_name"], rows, f"Зарегистрировано: {count} из {count}."))
