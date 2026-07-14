import sqlite3
from datetime import datetime, timedelta

import pytest

from archive.core.base import ActivityItem, ActivityMeta, Target, TargetType
from archive.storage import SQLiteStore, make_archive_dedupe_key


def make_item(item_id: str, link: str | None = None) -> ActivityItem:
    """构造可入库的归档任务 payload。"""
    acted_at = datetime(2026, 7, 14, 12, 0, 0)
    return ActivityItem(
        id=item_id,
        target=Target(
            title="测试回答",
            link=link or "https://www.zhihu.com/question/1/answer/2?utm=a",
            author="someone",
            fetched_at=acted_at,
        ),
        meta=ActivityMeta(
            action="赞同",
            target_type=TargetType.ANSWER,
            acted_at=acted_at,
            raw=["赞同了回答", "2026-07-14 12:00"],
        ),
        people="someone",
    )


@pytest.mark.asyncio
async def test_schema_v1_removes_login_task_state_path(tmp_path) -> None:
    """验证 v1 数据库升级后移除登录任务中的废弃绝对路径。"""
    database_path = tmp_path / "zhi.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            CREATE TABLE login_tasks (
                id TEXT PRIMARY KEY,
                qrcode_path TEXT NOT NULL,
                state_path TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            INSERT INTO login_tasks
                (id, qrcode_path, state_path, status, created_at, expires_at)
            VALUES
                ('login-id', '/tmp/code.png', '/host/state.json', 'failed',
                 '2026-07-15T08:00:00', '2026-07-15T08:05:00');
            PRAGMA user_version=1;
            """
        )
    store = SQLiteStore(database_path)

    await store.connect()
    connection = await store._connection()
    columns_cursor = await connection.execute("PRAGMA table_info(login_tasks)")
    columns = await columns_cursor.fetchall()
    row = await (
        await connection.execute(
            "SELECT id, qrcode_path, status FROM login_tasks WHERE id = 'login-id'"
        )
    ).fetchone()
    version = await (await connection.execute("PRAGMA user_version")).fetchone()

    assert "state_path" not in {column["name"] for column in columns}
    assert dict(row) == {
        "id": "login-id",
        "qrcode_path": "/tmp/code.png",
        "status": "failed",
    }
    assert version[0] == 2
    await store.close()


@pytest.mark.asyncio
async def test_seed_defaults_does_not_overwrite_saved_settings(tmp_path) -> None:
    """验证首次默认值不会覆盖用户已保存的运行时配置。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.seed_defaults()
    await store.set_settings("global", {"people": "saved-user"})

    await store.seed_defaults()

    assert (await store.get_settings("global"))["people"] == "saved-user"
    await store.close()


@pytest.mark.asyncio
async def test_monitor_enqueue_dedupes_and_updates_checkpoint(tmp_path) -> None:
    """验证 monitor 任务按稳定键去重，并与 checkpoint 一起提交。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    item = make_item("random-id-1")
    duplicate = make_item("random-id-2")
    assert make_archive_dedupe_key(item) == make_archive_dedupe_key(duplicate)
    checkpoint = datetime(2026, 7, 14, 12, 0, 0)

    inserted = await store.enqueue_monitor_items_and_checkpoint(
        "someone",
        [item, duplicate],
        checkpoint,
        checkpoint,
    )
    task = await store.claim_archive_task()
    current_checkpoint = await store.get_monitor_checkpoint("someone")

    assert inserted == 1
    assert task["payload"]["id"] == "random-id-1"
    assert current_checkpoint["fetch_until"] == checkpoint
    await store.close()


@pytest.mark.asyncio
async def test_running_tasks_are_recovered_on_startup(tmp_path) -> None:
    """验证遗留 running 任务会在启动恢复为 pending。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.enqueue_archive_item(make_item("task-id"))
    claimed = await store.claim_archive_task()
    assert claimed["id"] == "task-id"

    await store.recover_running_archive_tasks()

    recovered = await store.claim_archive_task()
    assert recovered["id"] == "task-id"
    await store.close()


@pytest.mark.asyncio
async def test_failed_archive_task_retries_then_stops(tmp_path) -> None:
    """验证归档任务失败后按简单重试策略进入 pending 和 failed。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.enqueue_archive_item(make_item("task-id"))
    await store.claim_archive_task()

    await store.mark_archive_task_failed("task-id", "timeout")
    await store.requeue_archive_task("task-id")
    for _ in range(3):
        await store.claim_archive_task()
        await store.mark_archive_task_failed("task-id", "timeout")

    assert not await store.has_pending_archive_tasks()
    await store.close()


@pytest.mark.asyncio
async def test_login_tasks_fail_on_restart(tmp_path) -> None:
    """验证未完成的登录任务会在应用启动恢复时标记为失败。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.create_login_task(
        "login-id",
        tmp_path / "login.qrcode.png",
        datetime.now() + timedelta(minutes=5),
    )

    await store.fail_incomplete_login_tasks()

    assert await store.get_login_task_status("login-id") == "failed"
    await store.close()


@pytest.mark.asyncio
async def test_create_login_task_reuses_active_task(tmp_path) -> None:
    """验证已有活跃登录任务时不会创建第二个 pending 任务。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    expires_at = datetime.now() + timedelta(minutes=5)
    first = await store.create_login_task(
        "first",
        tmp_path / "first.qrcode.png",
        expires_at,
    )

    second = await store.create_login_task(
        "second",
        tmp_path / "second.qrcode.png",
        expires_at,
    )

    assert first["id"] == "first"
    assert second["id"] == "first"
    assert await store.get_login_task_status("second") == "not_exist"
    await store.close()
