"""Восстановление: действие сохраняется в новой базе до запуска бота.

Команды Docker подменены: настоящая база, копии и контейнеры не затрагиваются.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "update.sh"
ACTION = "Восстановление из резервной копии"
DOCKER = """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
sql = sys.stdin.read() if "psql" in args else ""
with open(os.environ["DOCKER_CALLS"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"args": args, "sql": sql}, ensure_ascii=False) + "\\n")
if args[0] == "inspect":
    print("running 0")
elif "ps" in args:
    print("container-id")
elif "version" in args:
    print("0.0.0")
stage = os.environ.get("RESTORE_FAILURE", "")
if stage == "validate" and "pg_restore" in args:
    sys.exit(1)
if stage == "restore" and "sh" in args and "pg_restore" in args[-1]:
    sys.exit(1)
if stage == "audit" and "psql" in args:
    sys.exit(1)
"""


def restore(
    tmp_path: Path, *, failure: str = "", answer: str = "да"
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]], str]:
    """Запустить настоящий update.sh с подменой только внешних команд."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copyfile(SCRIPT, deploy / "update.sh")
    (deploy / ".env").write_text("BOT_VERSION=0.0.0\n", encoding="utf-8")
    commands = tmp_path / "commands"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    sleep = commands / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    # Апостроф и SQL в имени не должны исполняться ни оболочкой, ни psql.
    dump = "backups/база-'); DROP TABLE audit; --.dump"
    result = subprocess.run(
        ["bash", str(deploy / "update.sh"), "--restore", dump],
        input=f"{answer}\n",
        text=True,
        capture_output=True,
        timeout=10,
        env={
            **os.environ,
            "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_CALLS": str(calls),
            "RESTORE_FAILURE": failure,
        },
        check=False,
    )
    return result, [json.loads(line) for line in calls.read_text().splitlines()], dump


def test_restore_records_one_action_in_restored_database_before_bot_starts(tmp_path: Path) -> None:
    result, calls, dump = restore(tmp_path)
    assert result.returncode == 0, result.stderr
    restored = next(i for i, call in enumerate(calls) if "pg_restore --no-owner" in str(call))
    audited = [i for i, call in enumerate(calls) if ACTION in str(call["sql"])]
    started = next(i for i, call in enumerate(calls) if call["args"] == ["compose", "start", "bot"])
    assert len(audited) == 1
    assert restored < audited[0] < started
    sql = str(calls[audited[0]]["sql"])
    assert "INSERT INTO audit (at, actor_id, actor_fio, action, details)" in sql
    assert "0, 'администратор сервера'" in sql
    assert "'файл: ' || :'dump'" in sql
    assert dump not in sql
    assert f"dump={dump}" in calls[audited[0]]["args"]
    # Отметка от повторной обработки старых сообщений и действие коммитятся вместе.
    assert "'restored_at', :'stopped'" in sql
    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")


@pytest.mark.parametrize("failure", ["validate", "restore"])
def test_failed_restore_does_not_record_action_or_start_bot(tmp_path: Path, failure: str) -> None:
    result, calls, _ = restore(tmp_path, failure=failure)
    assert result.returncode != 0
    assert not any(ACTION in str(call["sql"]) for call in calls)
    assert not any(call["args"] == ["compose", "start", "bot"] for call in calls)


def test_failed_audit_write_keeps_bot_stopped(tmp_path: Path) -> None:
    result, calls, _ = restore(tmp_path, failure="audit")
    assert result.returncode != 0
    assert "запись журнала действий не сохранены" in result.stderr
    assert not any(call["args"] == ["compose", "start", "bot"] for call in calls)


def test_canceling_restore_does_not_record_action_or_stop_bot(tmp_path: Path) -> None:
    result, calls, _ = restore(tmp_path, answer="нет")
    assert result.returncode != 0
    assert not any(ACTION in str(call["sql"]) for call in calls)
    assert not any(call["args"] == ["compose", "stop", "bot"] for call in calls)
