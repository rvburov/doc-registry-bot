"""В репозитории нет названий сторонних программ: только родовые имена (AGENTS.md).

Сами названия записаны задом наперед, чтобы их не было и в этом файле.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
}
_FORBIDDEN = ("esabtrams", "сйебтрамс", "kniltsop")


def _text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            files.append(path)
    return files


def _found(text: str) -> list[str]:
    squeezed = re.sub(r"[\s_.-]+", "", text.casefold())
    return [word[::-1] for word in _FORBIDDEN if word[::-1] in squeezed]


def test_the_check_sees_a_name_written_with_spaces_or_dashes() -> None:
    word = _FORBIDDEN[0][::-1]
    assert _found(f"x {word[:5].title()}-{word[5:].title()} y") == [word]
    assert _found(f"x {word[:5]} {word[5:].upper()} y") == [word]
    assert _found("служба мессенджера") == []


def test_no_third_party_program_names_in_the_repository() -> None:
    hits: list[str] = []
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for word in _found(text):
            hits.append(f"{path.relative_to(ROOT)}: {word}")
    assert not hits, "Названия сторонних программ в репозитории:\n" + "\n".join(hits)
