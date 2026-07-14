from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from archive.core.base import Action, ActivityItem, TargetType, get_correct_target_type
from archive.core.monitor import Monitor


@pytest.mark.parametrize(
    ("action", "target_type"),
    [
        (Action.AGREE, TargetType.PIN),
        (Action.POST_PIN, TargetType.PIN),
    ],
)
def test_get_correct_target_type_supports_pin(
    action: Action,
    target_type: TargetType,
) -> None:
    """验证赞同和发布的想法都能被识别。"""
    assert get_correct_target_type(action.value, target_type.value) == TargetType.PIN


@pytest.mark.asyncio
async def test_extract_pin_title_uses_playwright_python_api() -> None:
    """验证通过 Playwright Python 接口提取并清理想法正文。"""
    content_locator = MagicMock()
    content_locator.inner_text = AsyncMock(return_value="想法正文\u200b ")
    target_locator = MagicMock()
    target_locator.locator.return_value = content_locator
    monitor = Monitor.__new__(Monitor)

    title = await monitor.extract_pin_title(target_locator)

    assert title == "想法正文"
    target_locator.locator.assert_called_once_with("span.RichText.ztext")
    content_locator.inner_text.assert_awaited_once_with(timeout=1000)


@pytest.mark.asyncio
async def test_expand_pin_content_clicks_read_more() -> None:
    """验证长想法存在“阅读全文”时会在截图前展开。"""
    read_more = MagicMock()
    read_more.is_visible = AsyncMock(return_value=True)
    read_more.click = AsyncMock()
    read_more_locators = MagicMock()
    read_more_locators.count = AsyncMock(return_value=1)
    read_more_locators.nth.return_value = read_more
    item_locator = MagicMock()
    item_locator.locator.return_value = read_more_locators
    monitor = Monitor.__new__(Monitor)

    expanded = await monitor.expand_pin_content(item_locator)

    assert expanded is True
    read_more.click.assert_awaited_once_with(timeout=1000)


@pytest.mark.asyncio
async def test_prepare_pin_warns_when_read_more_click_fails() -> None:
    """验证想法全文展开失败时会记录警告并继续处理。"""
    item_locator = MagicMock()
    item_locator.scroll_into_view_if_needed = AsyncMock()
    monitor = Monitor.__new__(Monitor)
    monitor.expand_pin_content = AsyncMock(return_value=False)
    monitor.logger = MagicMock()

    await monitor.prepare_item_for_screenshot(item_locator, TargetType.PIN)

    monitor.logger.warning.assert_called_once()
    item_locator.scroll_into_view_if_needed.assert_awaited_once()


def test_filter_archivable_items_excludes_pin() -> None:
    """验证想法只由 monitor 保存，不会进入 archiver 队列。"""
    now = datetime.now()
    pin_item = ActivityItem(
        id="pin",
        target={
            "title": "想法正文",
            "link": "",
            "author": "author",
            "fetched_at": now,
        },
        meta={
            "action": Action.AGREE,
            "target_type": TargetType.PIN,
            "acted_at": now,
            "raw": ["赞同了想法"],
        },
        people="someone",
    )
    answer_item = ActivityItem(
        id="answer",
        target={
            "title": "回答标题",
            "link": "/question/1/answer/2",
            "author": "author",
            "fetched_at": now,
        },
        meta={
            "action": Action.AGREE,
            "target_type": TargetType.ANSWER,
            "acted_at": now,
            "raw": ["赞同了回答"],
        },
        people="someone",
    )
    monitor = Monitor.__new__(Monitor)

    assert monitor.filter_archivable_items([pin_item, answer_item]) == [answer_item]


@pytest.mark.asyncio
async def test_save_and_push_updates_checkpoint_for_empty_items() -> None:
    """验证没有可保存动态时仍会推进 monitor checkpoint。"""
    now = datetime.now()
    monitor = Monitor.__new__(Monitor)
    monitor.people = "someone"
    monitor.fetch_until = now
    monitor.latest_dt = now
    monitor.logger = MagicMock()
    monitor.sqlite_store = MagicMock()
    monitor.sqlite_store.enqueue_monitor_items_and_checkpoint = AsyncMock(
        return_value=0
    )

    await monitor.save_and_push([])

    monitor.sqlite_store.enqueue_monitor_items_and_checkpoint.assert_awaited_once_with(
        "someone",
        [],
        now,
        now,
    )
