import asyncio
import json
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator
from urllib import parse

import aiosqlite

from archive.config import settings
from archive.utils.common import dt_fromisoformat, dt_toisoformat
from archive.utils.encoder import JSONEncoder

SCHEMA_VERSION = 2
TASK_RETRY_DELAYS = [timedelta(seconds=30), timedelta(minutes=5)]
MAX_TASK_ATTEMPTS = 3
ActivityItem = dict[str, Any]


class WorkerBusyError(Exception):
    """表示 worker 当前不满足安全修改条件。"""


def utcnow_iso() -> str:
    """生成用于 SQLite 记录的当前时间字符串。"""
    return datetime.now().isoformat(timespec="seconds")


def normalize_task_link(link: str) -> str:
    """标准化归档任务链接，用于生成稳定去重键。"""
    value = (link or "").strip()
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = parse.urlparse(value)
    path = parsed.path.rstrip("/") or "/"
    return parse.urlunparse(
        (
            parsed.scheme.lower() or "https",
            parsed.netloc.lower(),
            path,
            "",
            "",
            "",
        )
    )


def make_archive_dedupe_key(item: ActivityItem) -> str:
    """根据动态核心字段生成 monitor 任务的稳定去重键。"""
    meta = item["meta"]
    return "|".join(
        [
            str(item.get("people") or ""),
            dt_toisoformat(dt_fromisoformat(meta["acted_at"])),
            str(meta["action"]),
            str(meta["target_type"]),
            normalize_task_link(item["target"]["link"]),
        ]
    )


class SQLiteStore:
    """封装 ZhiArchive 单进程运行所需的 SQLite 持久化操作。"""

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        """创建 SQLite 存储对象。

        Args:
            path: SQLite 数据库文件路径，默认读取 Settings。
        """
        self.path = pathlib.Path(path or settings.sqlite_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """打开数据库连接并初始化 schema。"""
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.commit()
        await self._init_schema()

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db is None:
            return
        await self._db.close()
        self._db = None

    async def ping(self) -> bool:
        """检查 SQLite 连接是否可执行查询。"""
        try:
            db = await self._connection()
            await (await db.execute("SELECT 1")).fetchone()
            return True
        except Exception:
            return False

    async def _connection(self) -> aiosqlite.Connection:
        """返回已初始化的数据库连接。"""
        await self.connect()
        assert self._db is not None
        return self._db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """以短事务执行一组 SQLite 写操作。"""
        db = await self._connection()
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()

    async def _init_schema(self) -> None:
        """创建或校验当前版本的 SQLite schema。"""
        db = await self._connection_without_init()
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLite schema version {version} is newer than supported "
                f"version {SCHEMA_VERSION}."
            )
        if version == 0:
            await self._create_schema(db)
            await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await db.commit()
        elif version < SCHEMA_VERSION:
            await self._migrate_schema(db, version)

    async def _connection_without_init(self) -> aiosqlite.Connection:
        """返回内部连接，供 schema 初始化阶段使用。"""
        assert self._db is not None
        return self._db

    async def _create_schema(self, db: aiosqlite.Connection) -> None:
        """创建当前版本的数据表。"""
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS worker_control (
                name TEXT PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'waiting',
                last_error TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS archive_tasks (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_archive_tasks_claim
            ON archive_tasks(status, next_attempt_at, created_at, id);

            CREATE TABLE IF NOT EXISTS monitor_checkpoints (
                people TEXT PRIMARY KEY,
                fetch_until TEXT NOT NULL,
                latest_dt TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_tasks (
                id TEXT PRIMARY KEY,
                qrcode_path TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )

    async def _migrate_schema(
        self,
        db: aiosqlite.Connection,
        version: int,
    ) -> None:
        """按版本顺序迁移既有 SQLite schema。

        Args:
            db: 当前 SQLite 连接。
            version: 数据库当前 schema 版本。
        """
        if version != 1:
            raise RuntimeError(f"Unsupported SQLite schema version: {version}")
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                """
                CREATE TABLE login_tasks_v2 (
                    id TEXT PRIMARY KEY,
                    qrcode_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                INSERT INTO login_tasks_v2
                    (id, qrcode_path, status, last_error, created_at, expires_at)
                SELECT id, qrcode_path, status, last_error, created_at, expires_at
                FROM login_tasks
                """
            )
            await db.execute("DROP TABLE login_tasks")
            await db.execute("ALTER TABLE login_tasks_v2 RENAME TO login_tasks")
            await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            await db.rollback()
            raise
        else:
            await db.commit()

    async def seed_defaults(self) -> None:
        """首次建库时写入运行时默认配置和 worker 控制行。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                ("global:people", json.dumps(settings.people, cls=JSONEncoder), now),
            )
            for name in ("monitor", "archiver"):
                await db.execute(
                    """
                    INSERT OR IGNORE INTO worker_control
                        (name, paused, status, last_error, updated_at)
                    VALUES (?, 1, 'waiting', NULL, ?)
                    """,
                    (name, now),
                )
            await self.ensure_monitor_checkpoint(settings.people, db=db)

    async def get_settings(self, prefix: str) -> dict[str, Any]:
        """读取指定前缀下的配置值。"""
        db = await self._connection()
        cursor = await db.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (f"{prefix}:%",),
        )
        rows = await cursor.fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            key = str(row["key"]).split(":", maxsplit=1)[1]
            result[key] = json.loads(row["value"])
        return result

    async def set_settings(self, prefix: str, values: dict[str, Any]) -> None:
        """原子写入指定前缀下的一组配置值。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            for key, value in values.items():
                await db.execute(
                    """
                    INSERT INTO settings(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (
                        f"{prefix}:{key}",
                        json.dumps(value, ensure_ascii=False, cls=JSONEncoder),
                        now,
                    ),
                )

    async def delete_settings(self, prefix: str, keys: list[str]) -> None:
        """删除指定前缀下的一组配置值。

        Args:
            prefix: 配置键前缀。
            keys: 需要删除的配置项名称。
        """
        if not keys:
            return
        placeholders = ", ".join("?" for _ in keys)
        values = [f"{prefix}:{key}" for key in keys]
        async with self.transaction() as db:
            await db.execute(
                f"DELETE FROM settings WHERE key IN ({placeholders})",  # noqa: S608
                values,
            )

    async def set_worker_status(
        self,
        name: str,
        status: str,
        last_error: str | None = None,
    ) -> None:
        """更新 worker 展示状态。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO worker_control(name, paused, status, last_error, updated_at)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (name, status, last_error, now),
            )

    async def get_worker_status(self, name: str) -> str:
        """读取 worker 当前展示状态。"""
        db = await self._connection()
        row = await (
            await db.execute(
                "SELECT status FROM worker_control WHERE name = ?",
                (name,),
            )
        ).fetchone()
        return row["status"] if row else "waiting"

    async def set_worker_paused(self, name: str, paused: bool) -> None:
        """持久化 worker 暂停状态。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO worker_control(name, paused, status, updated_at)
                VALUES (?, ?, 'waiting', ?)
                ON CONFLICT(name) DO UPDATE SET
                    paused = excluded.paused,
                    updated_at = excluded.updated_at
                """,
                (name, int(paused), now),
            )

    async def get_worker_paused(self, name: str) -> bool:
        """读取 worker 是否处于暂停状态。"""
        db = await self._connection()
        row = await (
            await db.execute(
                "SELECT paused FROM worker_control WHERE name = ?",
                (name,),
            )
        ).fetchone()
        return bool(row["paused"]) if row else True

    async def ensure_monitor_checkpoint(
        self,
        people: str,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        """确保目标用户存在抓取检查点。"""
        owns_transaction = db is None
        if db is None:
            db = await self._connection()
        default_until = datetime.now() - timedelta(days=settings.monitor_fetch_until)
        now = utcnow_iso()
        sql = """
            INSERT OR IGNORE INTO monitor_checkpoints
                (people, fetch_until, latest_dt, updated_at)
            VALUES (?, ?, ?, ?)
        """
        if owns_transaction:
            async with self.transaction() as txn:
                await txn.execute(
                    sql,
                    (people, dt_toisoformat(default_until), None, now),
                )
        else:
            await db.execute(sql, (people, dt_toisoformat(default_until), None, now))

    async def get_monitor_checkpoint(self, people: str) -> dict[str, datetime | None]:
        """读取目标用户的 monitor 抓取检查点。"""
        await self.ensure_monitor_checkpoint(people)
        db = await self._connection()
        row = await (
            await db.execute(
                """
                SELECT fetch_until, latest_dt
                FROM monitor_checkpoints
                WHERE people = ?
                """,
                (people,),
            )
        ).fetchone()
        return {
            "fetch_until": dt_fromisoformat(row["fetch_until"]),
            "latest_dt": dt_fromisoformat(row["latest_dt"])
            if row["latest_dt"]
            else None,
        }

    async def set_monitor_checkpoint(
        self,
        people: str,
        fetch_until: datetime,
        latest_dt: datetime | None = None,
    ) -> None:
        """写入目标用户的 monitor 抓取检查点。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO monitor_checkpoints
                    (people, fetch_until, latest_dt, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(people) DO UPDATE SET
                    fetch_until = excluded.fetch_until,
                    latest_dt = excluded.latest_dt,
                    updated_at = excluded.updated_at
                """,
                (
                    people,
                    dt_toisoformat(fetch_until),
                    dt_toisoformat(latest_dt) if latest_dt else None,
                    now,
                ),
            )

    async def set_monitor_checkpoint_if_idle(
        self,
        people: str,
        fetch_until: datetime,
    ) -> dict[str, datetime | None]:
        """在同一事务中确认 Monitor 空闲并更新抓取检查点。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    """
                    SELECT paused, status
                    FROM worker_control
                    WHERE name = 'monitor'
                    """
                )
            ).fetchone()
            paused = bool(row["paused"]) if row else True
            status = row["status"] if row else "waiting"
            if not paused or status == "running":
                raise WorkerBusyError("monitor is running or not paused")

            checkpoint = await (
                await db.execute(
                    """
                    SELECT latest_dt
                    FROM monitor_checkpoints
                    WHERE people = ?
                    """,
                    (people,),
                )
            ).fetchone()
            latest_dt = checkpoint["latest_dt"] if checkpoint else None
            await db.execute(
                """
                INSERT INTO monitor_checkpoints
                    (people, fetch_until, latest_dt, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(people) DO UPDATE SET
                    fetch_until = excluded.fetch_until,
                    latest_dt = excluded.latest_dt,
                    updated_at = excluded.updated_at
                """,
                (
                    people,
                    dt_toisoformat(fetch_until),
                    latest_dt,
                    now,
                ),
            )
        return await self.get_monitor_checkpoint(people)

    async def enqueue_archive_item(
        self,
        item: ActivityItem,
        dedupe_key: str | None = None,
        db: aiosqlite.Connection | None = None,
    ) -> bool:
        """把单条归档 payload 写入任务表。"""
        now = utcnow_iso()
        payload = json.dumps(item, ensure_ascii=False, cls=JSONEncoder)
        sql = """
            INSERT OR IGNORE INTO archive_tasks
                (id, payload, dedupe_key, status, attempts, created_at)
            VALUES (?, ?, ?, 'pending', 0, ?)
        """
        params = (item["id"], payload, dedupe_key, now)
        if db is not None:
            cursor = await db.execute(sql, params)
            return cursor.rowcount > 0
        async with self.transaction() as txn:
            cursor = await txn.execute(sql, params)
            return cursor.rowcount > 0

    async def enqueue_monitor_items_and_checkpoint(
        self,
        people: str,
        items: list[ActivityItem],
        fetch_until: datetime,
        latest_dt: datetime | None,
    ) -> int:
        """在同一事务中入队 monitor 任务并推进抓取检查点。"""
        inserted = 0
        now = utcnow_iso()
        async with self.transaction() as db:
            for item in items:
                if await self.enqueue_archive_item(
                    item,
                    dedupe_key=make_archive_dedupe_key(item),
                    db=db,
                ):
                    inserted += 1
            await db.execute(
                """
                INSERT INTO monitor_checkpoints
                    (people, fetch_until, latest_dt, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(people) DO UPDATE SET
                    fetch_until = excluded.fetch_until,
                    latest_dt = excluded.latest_dt,
                    updated_at = excluded.updated_at
                """,
                (
                    people,
                    dt_toisoformat(fetch_until),
                    dt_toisoformat(latest_dt) if latest_dt else None,
                    now,
                ),
            )
        return inserted

    async def recover_running_archive_tasks(self) -> None:
        """启动时把遗留 running 归档任务恢复为 pending。"""
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE archive_tasks
                SET status = 'pending', started_at = NULL
                WHERE status = 'running'
                """
            )

    async def has_pending_archive_tasks(self) -> bool:
        """检查是否存在可执行归档任务。"""
        db = await self._connection()
        now = utcnow_iso()
        row = await (
            await db.execute(
                """
                SELECT 1 FROM archive_tasks
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                LIMIT 1
                """,
                (now,),
            )
        ).fetchone()
        return row is not None

    async def claim_archive_task(self) -> dict[str, Any] | None:
        """用事务领取最早可执行的一条 pending 任务。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            row = await (
                await db.execute(
                    """
                    SELECT id, payload, attempts
                    FROM archive_tasks
                    WHERE status = 'pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY created_at, id
                    LIMIT 1
                    """,
                    (now,),
                )
            ).fetchone()
            if row is None:
                return None
            await db.execute(
                """
                UPDATE archive_tasks
                SET status = 'running', started_at = ?, last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (now, row["id"]),
            )
            return {
                "id": row["id"],
                "payload": json.loads(row["payload"]),
                "attempts": row["attempts"],
            }

    async def mark_archive_task_done(self, task_id: str) -> None:
        """把归档任务标记为完成。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE archive_tasks
                SET status = 'done', finished_at = ?, next_attempt_at = NULL
                WHERE id = ?
                """,
                (now, task_id),
            )

    async def mark_archive_task_failed(self, task_id: str, error: str) -> None:
        """记录归档失败并按简单重试策略更新状态。"""
        db = await self._connection()
        row = await (
            await db.execute(
                "SELECT attempts FROM archive_tasks WHERE id = ?",
                (task_id,),
            )
        ).fetchone()
        attempts = int(row["attempts"]) + 1 if row else 1
        status = "failed" if attempts >= MAX_TASK_ATTEMPTS else "pending"
        delay = TASK_RETRY_DELAYS[min(attempts - 1, len(TASK_RETRY_DELAYS) - 1)]
        next_attempt_at = (
            None
            if status == "failed"
            else (datetime.now() + delay).isoformat(timespec="seconds")
        )
        finished_at = utcnow_iso() if status == "failed" else None
        async with self.transaction() as txn:
            await txn.execute(
                """
                UPDATE archive_tasks
                SET status = ?,
                    attempts = ?,
                    last_error = ?,
                    next_attempt_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (status, attempts, error, next_attempt_at, finished_at, task_id),
            )

    async def requeue_archive_task(self, task_id: str) -> None:
        """手动把失败任务重新入队。"""
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE archive_tasks
                SET status = 'pending',
                    attempts = 0,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    started_at = NULL,
                    finished_at = NULL
                WHERE id = ?
                """,
                (task_id,),
            )

    async def create_login_task(
        self,
        task_id: str,
        qrcode_path: pathlib.Path,
        expires_at: datetime,
    ) -> dict[str, Any]:
        """创建二维码登录任务，若已有活跃任务则返回既有任务。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE login_tasks
                SET status = 'failed',
                    last_error = COALESCE(last_error, '登录任务已过期')
                WHERE status IN ('pending', 'waiting_for_scan')
                  AND expires_at <= ?
                """,
                (now,),
            )
            row = await (
                await db.execute(
                    """
                    SELECT id, qrcode_path, status
                    FROM login_tasks
                    WHERE status IN ('pending', 'waiting_for_scan')
                    ORDER BY created_at
                    LIMIT 1
                    """
                )
            ).fetchone()
            if row is not None:
                return dict(row)
            await db.execute(
                """
                INSERT INTO login_tasks
                    (id, qrcode_path, status, created_at, expires_at)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (
                    task_id,
                    str(qrcode_path),
                    now,
                    expires_at.isoformat(timespec="seconds"),
                ),
            )
        return {
            "id": task_id,
            "qrcode_path": str(qrcode_path),
            "status": "pending",
        }

    async def get_login_task(self, task_id: str) -> dict[str, Any] | None:
        """读取二维码登录任务。"""
        db = await self._connection()
        row = await (
            await db.execute(
                """
                SELECT id, qrcode_path, status, last_error
                FROM login_tasks
                WHERE id = ?
                """,
                (task_id,),
            )
        ).fetchone()
        return dict(row) if row else None

    async def set_login_task_status(
        self,
        task_id: str,
        status: str,
        last_error: str | None = None,
    ) -> None:
        """更新二维码登录任务状态。"""
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE login_tasks
                SET status = ?, last_error = ?
                WHERE id = ?
                """,
                (status, last_error, task_id),
            )

    async def get_login_task_status(self, task_id: str) -> str:
        """读取二维码登录任务状态。"""
        task = await self.get_login_task(task_id)
        return str(task["status"]) if task else "not_exist"

    async def fail_incomplete_login_tasks(self) -> None:
        """启动或关闭时把未完成登录任务标记为失败。"""
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE login_tasks
                SET status = 'failed',
                    last_error = COALESCE(last_error, '应用已重启或关闭，请重新登录')
                WHERE status IN ('pending', 'waiting_for_scan')
                """
            )

    async def fail_expired_login_tasks(self) -> None:
        """创建新任务前清理过期登录任务。"""
        now = utcnow_iso()
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE login_tasks
                SET status = 'failed',
                    last_error = COALESCE(last_error, '登录任务已过期')
                WHERE status IN ('pending', 'waiting_for_scan')
                  AND expires_at <= ?
                """,
                (now,),
            )


_default_store: SQLiteStore | None = None


def get_default_store() -> SQLiteStore:
    """返回进程内默认 SQLite store。"""
    global _default_store
    if _default_store is None:
        _default_store = SQLiteStore()
    return _default_store
