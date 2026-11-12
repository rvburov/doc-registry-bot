"""Папки для резервных копий: заранее разрешенные в `.env`, выбор владельца в чате.

Бот сам в папки не пишет и доступа к диску компьютера не имеет: копии пишет контейнер копий,
и только в папки, подключенные к нему в compose.yaml (BACKUP_DIR_1..3) и в сетевую папку
(BACKUP_SHARE). В чате владелец выбирает одну из них номером, именем или путем, можно с
подпапкой внутри разрешенной папки. Путь Windows (E:\\Копии) и путь WSL (/mnt/e/Копии) — одно
и то же: Docker в WSL видит диск E: как /mnt/e.
"""

import re
from dataclasses import dataclass

from doc_registry_bot.registry.text import fold

TERMINAL = "0"
SHARE = "share"
MAX_DEPTH = 5
MAX_PART = 100
# Знаки, которых не может быть в имени папки Windows (и управляющие).
_BAD_CHARS = set('<>:"|?*\\') | {chr(code) for code in range(32)}
_WINDOWS = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$")
_WSL = re.compile(r"^/mnt/([a-z])(?:/(.*))?$")


@dataclass(frozen=True)
class Folder:
    """Разрешенная папка: ключ для контейнера копий, имя и путь на компьютере."""

    key: str
    name: str
    host: str

    @property
    def shown_path(self) -> str:
        return shown_path(self.host)

    def label(self) -> str:
        return f"{self.name} ({self.shown_path})"


@dataclass(frozen=True)
class Choice:
    folder: Folder
    subdir: str = ""

    def where(self) -> str:
        """Где окажутся файлы: путь на компьютере, как его увидит человек."""
        if not self.subdir:
            return self.folder.shown_path
        if self.folder.key == SHARE:
            return self.folder.shown_path.rstrip("/") + "/" + self.subdir
        separator = "\\" if _is_windows_like(self.folder.host) else "/"
        return (
            self.folder.shown_path.rstrip("\\/") + separator + self.subdir.replace("/", separator)
        )

    def label(self) -> str:
        return f"{self.folder.name} ({self.where()})"


def _is_windows_like(host: str) -> bool:
    return bool(_WSL.match(host.rstrip("/")))


def shown_path(host: str) -> str:
    """Путь для человека: /mnt/e/Копии показывается как E:\\Копии, остальное как есть."""
    value = host.strip()
    match = _WSL.match(value.rstrip("/") or "/")
    if match:
        drive, rest = match.group(1).upper(), match.group(2) or ""
        return f"{drive}:\\" + rest.replace("/", "\\")
    if value in ("./backups", "backups", "./backups/"):
        return "папка backups рядом с compose.yaml"
    return value


def normalized(path: str) -> str | None:
    """Путь к сравнению: E:\\a\\b, E:/a/b и /mnt/e/a/b дают /mnt/e/a/b. None — не путь."""
    value = path.strip().strip('"').strip("'").strip()
    if not value:
        return None
    match = _WINDOWS.match(value)
    if match:
        drive, rest = match.group(1).lower(), (match.group(2) or "").replace("\\", "/")
        value = f"/mnt/{drive}/{rest}"
    if not value.startswith("/"):
        return None
    parts = [part for part in value.split("/") if part]
    if parts[:1] == ["mnt"] and len(parts) >= 2 and len(parts[1]) == 1:
        parts[1] = parts[1].lower()
    return "/" + "/".join(parts)


def _unc(path: str) -> str | None:
    """Сетевой путь к сравнению: \\\\сервер\\папка и //сервер/папка одинаковы."""
    value = path.strip().strip('"').strip("'").replace("\\", "/").rstrip("/")
    if not value.startswith("//") or len(value) < 3:
        return None
    return fold(value)


def subdir_problem(subdir: str) -> str | None:
    """Почему подпапка не годится; None — годится."""
    parts = subdir.split("/")
    if len(parts) > MAX_DEPTH:
        return f"подпапок не больше {MAX_DEPTH} уровней"
    for part in parts:
        if part in ("", ".", ".."):
            return "в пути пустое имя, «.» или «..»"
        if part.startswith(".") or part.endswith((" ", ".")):
            return f"имя «{part}» не может начинаться с точки или кончаться пробелом или точкой"
        if len(part) > MAX_PART:
            return f"имя длиннее {MAX_PART} знаков"
        if any(char in _BAD_CHARS for char in part):
            return f"в имени «{part}» недопустимые знаки"
    return None


def choose(folders: list[Folder], answer: str) -> Choice | str:
    """Папка по ответу владельца: номер в списке, имя или путь (можно с подпапкой).

    Строка — ответ не подошел, в ней объяснение для человека.
    """
    value = answer.strip().rstrip(".").strip()
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(folders):
            return Choice(folders[index - 1])
        return f"Папка: номера {value} нет в списке"
    for folder in folders:
        if fold(folder.name) == fold(value):
            return Choice(folder)
    unc = _unc(value)
    for folder in folders:
        if folder.key == SHARE and unc is not None and unc == _unc(folder.host):
            return Choice(folder)
    path = normalized(value)
    if path is None:
        return f"Папка: «{value}» не номер, не имя и не путь из списка"
    for folder in folders:
        if folder.key == SHARE:
            continue
        base = normalized(folder.host)
        if base is None:
            continue
        # На дисках Windows (/mnt/e) регистр букв в именах папок не важен, как в Проводнике.
        windows = base.startswith("/mnt/")
        same = path.casefold() == base.casefold() if windows else path == base
        if same:
            return Choice(folder)
        prefix = base + "/"
        inside = (
            path.casefold().startswith(prefix.casefold()) if windows else path.startswith(prefix)
        )
        if inside:
            subdir = path[len(prefix) :]
            problem = subdir_problem(subdir)
            if problem:
                return f"Папка: {problem}"
            return Choice(folder, subdir)
    return (
        f"Папка «{value}» недоступна боту. Копии сохраняются только в папки из настроек "
        "сервера (список выше) и в их подпапки. Чтобы добавить папку, впишите ее путь в .env "
        "(BACKUP_DIR_2 или BACKUP_DIR_3) и выполните docker compose up -d"
    )
