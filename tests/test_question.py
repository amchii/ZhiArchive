import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from archive.core.question import (
    QuestionReadError,
    parse_question_initial_data,
    parse_question_url,
    read_question,
)


@pytest.mark.asyncio
async def test_read_question_returns_structured_result() -> None:
    """验证页面初始化问题对象会被清洗成结构化结果。"""
    initial_data = {
        "initialState": {
            "entities": {
                "questions": {
                    "123": {
                        "id": "123",
                        "title": "<b>测试问题</b>",
                        "detail": (
                            "<p>问题描述 <img src='https://pic.example/a.jpg'></p>"
                        ),
                        "author": {
                            "name": "提问机构",
                            "urlToken": "asker",
                            "isOrg": True,
                        },
                        "topics": [
                            {"id": 10, "name": "人工智能"},
                            {"id": 11, "name": "软件工程"},
                        ],
                        "created": 1_700_000_000,
                        "updatedTime": 1_700_000_100,
                        "answerCount": 8,
                        "followerCount": 9,
                        "visitCount": 10,
                        "commentCount": 2,
                    }
                }
            }
        }
    }
    locator = MagicMock()
    locator.wait_for = AsyncMock()
    locator.text_content = AsyncMock(return_value=json.dumps(initial_data))
    page = MagicMock()
    page.locator.return_value = locator

    result = await read_question(
        page,
        "https://www.zhihu.com/question/123?utm_source=test",
    )

    assert result.id == "123"
    assert result.title == "测试问题"
    assert result.detail == "问题描述"
    assert "<img" in result.detail_html
    assert result.author == "提问机构"
    assert result.author_url == "https://www.zhihu.com/org/asker"
    assert [topic.name for topic in result.topics] == ["人工智能", "软件工程"]
    assert result.topics[0].url == "https://www.zhihu.com/topic/10/hot"
    assert result.answer_count == 8
    assert result.follower_count == 9
    assert result.visit_count == 10
    assert result.comment_count == 2
    assert result.created_at is not None
    assert result.updated_at is not None
    page.locator.assert_called_once_with("#js-initialData")
    locator.wait_for.assert_awaited_once_with(state="attached", timeout=30_000)
    locator.text_content.assert_awaited_once_with(timeout=30_000)


def test_question_url_validation_rejects_answers_and_untrusted_hosts() -> None:
    """验证问题工具只接受固定知乎域名下的纯问题链接。"""
    assert parse_question_url("/question/123") == (
        "https://www.zhihu.com/question/123",
        "123",
    )

    for url in (
        "https://www.zhihu.com/question/123/answer/456",
        "https://evil.example/question/123",
        "https://www.zhihu.com/question/not-a-number",
        f"https://www.zhihu.com/question/{'1' * 31}",
        "https://www.zhihu.com:444/question/123",
    ):
        with pytest.raises(ValueError):
            parse_question_url(url)


@pytest.mark.asyncio
async def test_read_question_reports_missing_initial_data() -> None:
    """验证页面未提供初始化数据时返回明确错误。"""
    locator = MagicMock()
    locator.wait_for = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
    page = MagicMock()
    page.locator.return_value = locator

    with pytest.raises(QuestionReadError, match="初始化数据"):
        await read_question(page, "https://www.zhihu.com/question/123")


def test_parse_question_initial_data_rejects_invalid_or_missing_question() -> None:
    """验证损坏 JSON 和缺少目标问题对象时会失败。"""
    with pytest.raises(QuestionReadError, match="有效 JSON"):
        parse_question_initial_data("not-json", "123")

    raw = json.dumps({"initialState": {"entities": {"questions": {}}}})
    with pytest.raises(QuestionReadError, match="不存在"):
        parse_question_initial_data(raw, "123")
