"""Подделка контейнера копий: берет заявки из backup_jobs так же, как deploy/backup/backup.sh,
и пишет файлы в папки во временном каталоге (`root/<ключ папки>`).

Вместо настоящего DUMP пишется список обозначений журнала, прочитанный тем же снимком базы
(`SET TRANSACTION SNAPSHOT`), что и у настоящего pg_dump: так тесты видят, что DUMP и CSV
сняты в один момент.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

CLAIM = """
UPDATE backup_jobs SET status = 'running', started_at = now()
WHERE id = (SELECT id FROM backup_jobs WHERE status = 'new' ORDER BY id LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, kind, folder_key, subdir, dump_name, csv_name, snapshot, fingerprint,
          dump_sha256
"""

FINISH = """
UPDATE backup_jobs
SET status = :status, dump_ok = :dump_ok, csv_ok = :csv_ok, dump_size = :dump_size,
    csv_size = :csv_size, message = :message, dump_name = :dump_name, csv_name = :csv_name,
    dump_sha256 = :dump_sha256, host_path = :host_path,
    finished_at = now(), csv_data = NULL, snapshot = NULL
WHERE id = :id AND status = 'running'
"""


class FakeContainer:
    def __init__(self, db: async_sessionmaker[AsyncSession], root: Path) -> None:
        self.db = db
        self.root = root
        self.configured = {"0", "1", "2", "3"}
        self.share_ok = True
        self.fail_dump = False
        self.fail_csv = False
        self.before_dump: Callable[[], Awaitable[None]] | None = None
        self.before_verify: Callable[[], Awaitable[None]] | None = None
        self.done = 0
        self.errors: list[str] = []
        # Пути папок на компьютере из настроек контейнера: как BACKUP_DIR_N у настоящего.
        self.hosts: dict[str, str] = {}

    @property
    def engine(self) -> AsyncEngine:
        engine = self.db.kw["bind"]
        assert isinstance(engine, AsyncEngine)
        return engine

    def folder(self, key: str, subdir: str) -> Path:
        base = self.root / key
        return base / subdir if subdir else base

    async def step(self) -> bool:
        async with self.db() as session, session.begin():
            row = (await session.execute(text(CLAIM))).mappings().first()
        if row is None:
            return False
        job = dict(row)
        if job["kind"] == "backup":
            await self._backup(job)
        else:
            await self._verify(job)
        self.done += 1
        return True

    async def _finish(self, job: dict[str, Any], **values: Any) -> None:
        params = {
            "id": job["id"],
            "status": "failed",
            "dump_ok": False,
            "csv_ok": False,
            "dump_size": 0,
            "csv_size": 0,
            "message": "",
            "dump_name": job["dump_name"],
            "csv_name": job["csv_name"],
            "dump_sha256": job.get("dump_sha256") or "",
            "host_path": self.hosts.get(job["folder_key"], ""),
            **values,
        }
        async with self.db() as session, session.begin():
            await session.execute(text(FINISH), params)

    async def _backup(self, job: dict[str, Any]) -> None:
        key = job["folder_key"]
        if key == "share" and not self.share_ok:
            await self._finish(job, message="сетевая папка недоступна")
            return
        if key != "share" and key not in self.configured:
            await self._finish(job, message=f"папка {key} не подключена к контейнеру копий")
            return
        folder = self.folder(key, job["subdir"])
        folder.mkdir(parents=True, exist_ok=True)
        message = ""
        csv_ok = dump_ok = False
        # Как у настоящего: сначала DUMP снимком бота, CSV — только если DUMP получился.
        if self.before_dump is not None:
            await self.before_dump()
        if self.fail_dump:
            message = "DUMP: pg_dump: ошибка"
        else:
            rows = await self._snapshot_rows(job["snapshot"])
            dump = folder / job["dump_name"]
            dump.write_text(json.dumps(rows), encoding="utf-8")
            job["dump_sha256"] = hashlib.sha256(dump.read_bytes()).hexdigest()
            dump_ok = True
        async with self.db() as session:
            data = await session.scalar(
                text("SELECT csv_data FROM backup_jobs WHERE id = :id"), {"id": job["id"]}
            )
        fingerprint_ok = (
            bool(data) and hashlib.sha256(data or b"").hexdigest() == job["fingerprint"]
        )
        if dump_ok and not self.fail_csv and fingerprint_ok:
            (folder / job["csv_name"]).write_bytes(data or b"")
            csv_ok = True
        if dump_ok and not csv_ok:
            message = "CSV: не записан"
        await self._finish(
            job,
            status="done" if dump_ok and csv_ok else "failed",
            dump_ok=dump_ok,
            csv_ok=csv_ok,
            dump_size=(folder / job["dump_name"]).stat().st_size if dump_ok else 0,
            csv_size=len(data or b"") if csv_ok else 0,
            message=message.strip(),
        )

    async def _snapshot_rows(self, snapshot: str) -> list[str]:
        """То же, что сделал бы pg_dump --snapshot: журнал на момент снимка бота."""
        async with self.engine.connect() as connection:
            snap = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with snap.begin():
                await snap.execute(text(f"SET TRANSACTION SNAPSHOT '{snapshot}'"))
                result = await snap.execute(text("SELECT designation FROM journal ORDER BY id"))
                return [str(value) for value in result.scalars()]

    async def _verify(self, job: dict[str, Any]) -> None:
        if self.before_verify is not None:
            await self.before_verify()
        folder = self.folder(job["folder_key"], job["subdir"])
        csv = folder / job["csv_name"]
        dump = folder / job["dump_name"]
        csv_ok = csv.exists() and hashlib.sha256(csv.read_bytes()).hexdigest() == job["fingerprint"]
        dump_ok = (
            dump.exists() and hashlib.sha256(dump.read_bytes()).hexdigest() == job["dump_sha256"]
        )
        message = "" if csv_ok and dump_ok else "нет файла или не совпал отпечаток"
        await self._finish(
            job,
            status="done" if csv_ok and dump_ok else "failed",
            dump_ok=dump_ok,
            csv_ok=csv_ok,
            message=message,
        )

    async def alive(self) -> None:
        """Отметка «контейнер копий жив», как у настоящего (раз в 10 секунд)."""
        async with self.db() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO bot_state (key, value) VALUES ('backup_worker', now()::text) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                )
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.alive()
                if await self.step():
                    continue
            except Exception as error:
                self.errors.append(repr(error))
            await asyncio.sleep(0.01)
