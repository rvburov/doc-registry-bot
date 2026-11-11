"""Классификатор ЕСКД (ГОСТ 2.201-80): коды от двух до шести цифр с наименованиями.

Файл `data/eskd.tsv.gz`: строки отсортированы по коду КАК ПО СТРОКЕ, поэтому файл сам и есть
дерево: предок всегда стоит перед потомками. Узел ищется бинарным поиском, цепочка
предков — пятью поисками. При загрузке чинятся два дефекта исходной выгрузки (см. `_repair`).
"""

import bisect
import gzip
from dataclasses import dataclass
from functools import cache
from importlib import resources

CODE_LENGTH = 6
ROOT_LENGTH = 2


@dataclass(frozen=True)
class EskdEntry:
    """Строка классификатора: код (его длина — уровень) и наименование."""

    code: str
    name: str


@dataclass(frozen=True)
class _Catalog:
    entries: list[EskdEntry]
    codes: list[str]


def is_full_code(code: str | None) -> bool:
    """Код годится для обозначения: ровно шесть цифр."""
    return code is not None and len(code) == CODE_LENGTH and _ascii_digits(code)


def _ascii_digits(text: str) -> bool:
    return all(c in "0123456789" for c in text)


def _is_code(code: str | None) -> bool:
    return code is not None and ROOT_LENGTH <= len(code) <= CODE_LENGTH and _ascii_digits(code)


def node(code: str | None) -> EskdEntry | None:
    """Строка по точному коду любого уровня; у задвоенного шестизначного кода — первая."""
    if not _is_code(code):
        return None
    assert code is not None
    catalog = _load()
    at = bisect.bisect_left(catalog.codes, code)
    if at < len(catalog.codes) and catalog.codes[at] == code:
        return catalog.entries[at]
    return None


def find(code: str | None) -> EskdEntry | None:
    """Строка по точному шестизначному коду; None — такого кода нет."""
    return node(code) if is_full_code(code) else None


def chain(code: str | None) -> list[EskdEntry]:
    """Цепочка от класса до самого узла включительно. Пусто — кода нет."""
    if not _is_code(code):
        return []
    assert code is not None
    found = [node(code[:length]) for length in range(ROOT_LENGTH, len(code) + 1)]
    return [entry for entry in found if entry is not None]


def entries() -> list[EskdEntry]:
    """Весь справочник в порядке обхода дерева."""
    return _load().entries


@cache
def _load() -> _Catalog:
    repaired = _repair(_read())
    return _Catalog(repaired, [entry.code for entry in repaired])


def _read() -> list[EskdEntry]:
    data = resources.files("doc_registry_bot.registry").joinpath("data", "eskd.tsv.gz").read_bytes()
    rows: list[EskdEntry] = []
    for line in gzip.decompress(data).decode("utf-8").splitlines():
        tab = line.find("\t")
        if tab < ROOT_LENGTH or tab > CODE_LENGTH:
            continue
        code = line[:tab]
        if not _ascii_digits(code):
            continue
        name = line[tab + 1 :].strip()
        if not name:
            continue
        # Порядок — условие работы бинарного поиска и достройки узлов: сломанный порядок должен
        # назваться сам, а не превратиться в молча неполную выдачу.
        if rows and code < rows[-1].code:
            raise RuntimeError(
                f"Справочник ЕСКД не отсортирован по коду: {code} после {rows[-1].code}"
            )
        rows.append(EskdEntry(code, name))
    if not rows:
        raise RuntimeError("Справочник ЕСКД пуст: файл eskd.tsv.gz поврежден")
    return rows


def _repair(rows: list[EskdEntry]) -> list[EskdEntry]:
    """Два дефекта исходных данных, без починки которых дерево было бы неполным.

    1. Наименование, разорванное переносом при выгрузке (коды 4814 и 48414 задвоены): хвост
       приписывается к первой строке. Шестизначных это не касается: задвоенные там — разные строки.
    2. Пропущенный узел: у 26 шестизначных кодов нет пятизначного родителя. Он достраивается с
       пустым наименованием.
    """
    repaired: list[EskdEntry] = []
    last: list[str | None] = [None] * (CODE_LENGTH + 1)
    for row in rows:
        if len(row.code) < CODE_LENGTH and repaired and repaired[-1].code == row.code:
            head = repaired[-1]
            tail = row.name[:1].lower() + row.name[1:]
            repaired[-1] = EskdEntry(head.code, f"{head.name} {tail}")
            continue
        for length in range(ROOT_LENGTH, len(row.code)):
            parent = row.code[:length]
            if last[length] == parent:
                continue
            repaired.append(EskdEntry(parent, ""))
            last[length] = parent
        repaired.append(row)
        last[len(row.code)] = row.code
    return repaired
