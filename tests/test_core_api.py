import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from archive.api.endpoints.zhi import core
from archive.auth_state import AuthStateManager
from archive.core.base import TargetType, WorkStatus
from archive.core.monitor import Monitor
from archive.storage import SQLiteStore


def make_json_request(payload: object) -> Request:
    """构造可直接传给端点函数的 JSON Request。"""
    body = json.dumps(payload).encode()
    sent = False

    async def receive() -> dict[str, object]:
        """向 Starlette Request 提供一次请求体消息。"""
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/zhi/core/auth_state",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
    )


@pytest.mark.asyncio
async def test_upload_auth_state_returns_summary_without_server_path(
    monkeypatch,
    tmp_path,
) -> None:
    """验证上传接口启用 state 后只返回非敏感摘要。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    manager = AuthStateManager(store, tmp_path / "states/zhihu.state.json")
    monkeypatch.setattr(core, "get_auth_state_manager", lambda: manager)
    request = make_json_request(
        [
            {
                "name": "z_c0",
                "value": "secret",
                "domain": ".zhihu.com",
                "path": "/",
            }
        ]
    )

    result = await core.upload_auth_state(request)

    assert result["source"] == "upload"
    assert result["cookie_count"] == 1
    assert "path" not in result
    assert "cookies" not in result
    await store.close()


@pytest.mark.asyncio
async def test_enqueue_archive_task_returns_public_task_id(monkeypatch) -> None:
    """验证接口返回动态 ID，不暴露服务器任务文件路径。"""
    client = MagicMock()
    client.enqueue_url = AsyncMock(
        return_value=(
            MagicMock(task_name="/private/server/tasks/manual.json"),
            {
                "id": "activity-id",
                "target": {
                    "title": "",
                    "link": "https://www.zhihu.com/question/1/answer/2",
                    "author": "",
                    "fetched_at": "2026-07-09T12:00:00",
                },
                "meta": {
                    "action": "手动归档",
                    "target_type": TargetType.ANSWER,
                    "acted_at": "2026-07-09T12:00:00",
                    "raw": [],
                },
                "people": "someone",
            },
        )
    )
    monkeypatch.setattr(core, "get_api_client", lambda _name: client)

    result = await core.enqueue_archive_task(
        core.ArchiveURLRequest(
            url="https://www.zhihu.com/question/1/answer/2",
        )
    )

    assert result["task_id"] == "activity-id"
    assert "/private/server" not in result["task_id"]


@pytest.mark.asyncio
async def test_enqueue_archive_task_reports_invalid_url(monkeypatch) -> None:
    """验证无效链接会转换成可读的接口校验错误。"""
    client = MagicMock()
    client.enqueue_url = AsyncMock(side_effect=ValueError("不支持的链接"))
    monkeypatch.setattr(core, "get_api_client", lambda _name: client)

    with pytest.raises(HTTPException) as exc_info:
        await core.enqueue_archive_task(
            core.ArchiveURLRequest(url="https://example.com/answer/1")
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "不支持的链接"


@pytest.mark.asyncio
async def test_get_monitor_checkpoint_does_not_mutate_live_monitor(
    monkeypatch,
    tmp_path,
) -> None:
    """验证读取 checkpoint 不会修改正在运行的 Monitor 实例。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.seed_defaults()
    await store.set_settings("global", {"people": "new-user"})
    await store.set_monitor_checkpoint(
        "new-user",
        datetime(2026, 7, 14, 12, 0, 0),
    )
    live_monitor = Monitor(store=store)
    live_monitor.people = "old-user"
    monkeypatch.setattr(core, "get_store", lambda: store)

    result = await core.get_monitor_checkpoint()

    assert result["people"] == "new-user"
    assert live_monitor.people == "old-user"
    await store.close()


@pytest.mark.asyncio
async def test_set_monitor_checkpoint_requires_paused_monitor(
    monkeypatch,
    tmp_path,
) -> None:
    """验证运行中的 Monitor 不能被控制台覆盖 checkpoint。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.seed_defaults()
    await store.set_settings("global", {"people": "someone"})
    await store.set_worker_paused("monitor", False)
    await store.set_worker_status("monitor", WorkStatus.RUNNING.value)
    monkeypatch.setattr(core, "get_store", lambda: store)

    with pytest.raises(HTTPException) as exc_info:
        await core.set_monitor_checkpoint(
            core.MonitorCheckpointUpdate(
                fetch_until=datetime(2026, 7, 14, 12, 0, 0),
            )
        )

    assert exc_info.value.status_code == 409
    await store.close()
