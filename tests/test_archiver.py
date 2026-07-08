import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from archive.core.archiver import Archiver, parse_archive_url
from archive.core.base import Target, TargetType


@pytest.mark.parametrize(
    ("url", "expected_url", "target_type"),
    [
        (
            "https://www.zhihu.com/question/2058247449894970042/"
            "answer/2058308278786987158?utm_source=test#answer",
            "https://www.zhihu.com/question/2058247449894970042/"
            "answer/2058308278786987158",
            TargetType.ANSWER,
        ),
        (
            "http://zhuanlan.zhihu.com/p/123456/",
            "https://zhuanlan.zhihu.com/p/123456",
            TargetType.ARTICLE,
        ),
    ],
)
def test_parse_archive_url(
    url: str,
    expected_url: str,
    target_type: TargetType,
) -> None:
    """验证回答和文章链接会被识别并标准化。"""
    assert parse_archive_url(url) == (expected_url, target_type)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/question/1/answer/2",
        "https://www.zhihu.com/people/someone",
        "https://www.zhihu.com:8080/question/1/answer/2",
    ],
)
def test_parse_archive_url_rejects_unsupported_urls(url: str) -> None:
    """验证手动任务不能访问非目标页面或自定义端口。"""
    with pytest.raises(ValueError):
        parse_archive_url(url)


@pytest.mark.asyncio
async def test_enqueue_url_writes_compatible_archive_task(tmp_path) -> None:
    """验证手动链接会写入现有格式的任务文件并推送队列。"""
    archiver = Archiver.__new__(Archiver)
    archiver.people = "someone"
    archiver._base_results_dir = tmp_path
    archiver.global_configurator = MagicMock()
    archiver.global_configurator.load_to_worker = AsyncMock()
    archiver.push_task = AsyncMock()
    archiver.logger = MagicMock()

    task, item = await archiver.enqueue_url("https://www.zhihu.com/question/1/answer/2")

    task_items = json.loads(task.activity_path.read_text(encoding="utf-8"))
    assert task_items[0]["id"] == item["id"]
    assert task_items[0]["meta"]["action"] == "手动归档"
    assert task_items[0]["meta"]["target_type"] == TargetType.ANSWER
    assert task_items[0]["target"]["link"] == (
        "https://www.zhihu.com/question/1/answer/2"
    )
    archiver.global_configurator.load_to_worker.assert_awaited_once_with(sync=False)
    archiver.push_task.assert_awaited_once_with(task)


@pytest.mark.asyncio
async def test_fill_target_metadata_falls_back_to_page_title() -> None:
    """验证页面元数据缺失时仍能使用浏览器标题继续归档。"""
    title_locator = MagicMock()
    title_locator.get_attribute = AsyncMock(
        side_effect=PlaywrightError("缺少开放图谱标题")
    )
    author_locator = MagicMock()
    author_locator.first.get_attribute = AsyncMock(
        side_effect=PlaywrightError("缺少作者链接")
    )
    page = MagicMock()
    page.locator.side_effect = [title_locator, author_locator]
    page.title = AsyncMock(return_value="问题标题 - 知乎")
    target = Target(
        title="",
        link="https://www.zhihu.com/question/1/answer/2",
        author="",
        fetched_at="2026-07-09T12:00:00",
    )
    archiver = Archiver.__new__(Archiver)

    await archiver.fill_target_metadata(page, target, TargetType.ANSWER)

    assert target["title"] == "问题标题"
    assert target["author"] == ""


@pytest.mark.asyncio
async def test_prepare_page_injects_persistent_floating_action_style() -> None:
    """验证截图期间新出现的浮动操作栏也会被持续隐藏。"""
    page = MagicMock()
    page.evaluate = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    archiver = Archiver.__new__(Archiver)

    await archiver.prepare_page_for_screenshot(page)

    script = page.evaluate.await_args.args[0]
    assert 'styleId = "zhi-archive-screenshot-style"' in script
    assert ".ContentItem-actions.is-fixed" in script
    assert ".RichContent-actions.is-fixed" in script
    page.wait_for_timeout.assert_awaited_once_with(timeout=200)
