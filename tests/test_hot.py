from unittest.mock import AsyncMock, MagicMock

import pytest

from archive.core.hot import (
    HOT_LIST_API_URL,
    HOT_LIST_REFERER,
    HotListReadError,
    read_hot_questions,
    validate_hot_limit,
)
from archive.core.profile import ProfileRateLimitError


def make_hot_item(question_id: str, title: str) -> dict[str, object]:
    """构造热榜解析测试使用的单个问题条目。

    Args:
        question_id: 问题 ID。
        title: 问题标题。
    """
    return {
        "type": "hot_list_feed",
        "card_id": f"Q_{question_id}",
        "detail_text": "123 万热度",
        "debut": True,
        "trend": 0,
        "children": [{"type": "answer", "thumbnail": "https://pic.example/hot.jpg"}],
        "target": {
            "id": int(question_id),
            "title": title,
            "excerpt": "问题摘要",
            "answer_count": 12,
            "url": f"https://api.zhihu.com/questions/{question_id}",
        },
    }


@pytest.mark.asyncio
async def test_read_hot_questions_returns_structured_snapshot() -> None:
    """验证热榜 API 响应会转换为最多三十条问题。"""
    first = make_hot_item("123", "<b>第一条问题</b>")
    second = make_hot_item("456", "第二条问题")
    second_target = second["target"]
    assert isinstance(second_target, dict)
    second_target.pop("id")
    response = MagicMock(status=200, ok=True)
    response.json = AsyncMock(return_value={"data": [first, second], "paging": {}})
    request = MagicMock()
    request.get = AsyncMock(return_value=response)

    result = await read_hot_questions(request, limit=1)

    assert result.total == 2
    assert result.limit == 1
    assert len(result.items) == 1
    assert result.items[0].rank == 1
    assert result.items[0].id == "123"
    assert result.items[0].title == "第一条问题"
    assert result.items[0].excerpt == "问题摘要"
    assert result.items[0].heat == "123 万热度"
    assert result.items[0].answer_count == 12
    assert result.items[0].image_url == "https://pic.example/hot.jpg"
    assert result.items[0].label == "新"
    assert result.fetched_at is not None
    request.get.assert_awaited_once_with(
        HOT_LIST_API_URL,
        headers={"Referer": HOT_LIST_REFERER},
        timeout=30_000,
    )


def test_validate_hot_limit_rejects_values_outside_thirty() -> None:
    """验证热榜不接受分页或超过三十条的请求。"""
    assert validate_hot_limit(1) == 1
    assert validate_hot_limit(30) == 30
    for limit in (0, 31):
        with pytest.raises(ValueError, match="1 到 30"):
            validate_hot_limit(limit)


@pytest.mark.asyncio
async def test_read_hot_questions_maps_rate_limit_and_invalid_payload() -> None:
    """验证热榜读取会映射风控和异常响应。"""
    request = MagicMock()
    request.get = AsyncMock(
        return_value=MagicMock(
            status=429,
            ok=False,
            headers={"retry-after": "120"},
        )
    )

    with pytest.raises(ProfileRateLimitError) as error_info:
        await read_hot_questions(request)
    assert error_info.value.retry_after == 120

    response = MagicMock(status=200, ok=True)
    response.json = AsyncMock(return_value={"data": "invalid"})
    request.get = AsyncMock(return_value=response)
    with pytest.raises(HotListReadError, match="结构异常"):
        await read_hot_questions(request)
