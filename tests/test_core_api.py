from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from archive.api.endpoints.zhi import core
from archive.core.base import TargetType


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
