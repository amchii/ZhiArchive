import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from archive.core.archiver import (
    Archiver,
    TextArchive,
    format_text_archive_html,
    format_text_archive_markdown,
    parse_archive_url,
)
from archive.core.base import Target, TargetType


def make_text_archive() -> TextArchive:
    """构造文本归档测试数据。"""
    return TextArchive(
        title="测试回答",
        url="https://www.zhihu.com/question/1/answer/2",
        author="someone",
        author_url="https://www.zhihu.com/people/someone",
        published_at="2026-07-09T12:00:00.000Z",
        updated_at="",
        target_type="回答",
        html=(
            '<p><strong>正文</strong></p>'
            '<figure><img src="https://pic.example/a.jpg" alt="">'
            "<figcaption>图片说明</figcaption></figure>"
        ),
        markdown="**正文**\n\n![](https://pic.example/a.jpg)\n*图片说明*",
    )


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


def test_format_text_archive_html_wraps_clean_content() -> None:
    """验证 HTML 文本归档会包含正文、元数据和基础安全策略。"""
    archive = make_text_archive()

    content = format_text_archive_html(archive)

    assert "<title>测试回答</title>" in content
    assert "Content-Security-Policy" in content
    assert '<main class="zhi-archive-content">' in content
    assert "<strong>正文</strong>" in content
    assert "图片说明" in content
    assert "https://www.zhihu.com/question/1/answer/2" in content


def test_format_text_archive_markdown_wraps_metadata() -> None:
    """验证 Markdown 文本归档会包含标题、来源和正文。"""
    archive = make_text_archive()

    content = format_text_archive_markdown(archive)

    assert content.startswith("# 测试回答")
    assert "- 类型：回答" in content
    assert "- 作者：someone" in content
    assert "**正文**" in content
    assert "![](https://pic.example/a.jpg)" in content


@pytest.mark.asyncio
async def test_save_text_archive_writes_html_and_markdown(tmp_path) -> None:
    """验证文本归档会写入同目录的 HTML 和 Markdown 文件。"""
    archiver = Archiver.__new__(Archiver)
    archive = make_text_archive()

    files = await archiver.save_text_archive(tmp_path, "归档文件", archive)

    assert files == {
        "html": "归档文件.html",
        "markdown": "归档文件.md",
    }
    assert "<strong>正文</strong>" in tmp_path.joinpath("归档文件.html").read_text(
        encoding="utf-8"
    )
    assert "**正文**" in tmp_path.joinpath("归档文件.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_extract_text_archive_uses_page_dom() -> None:
    """验证文本归档抽取会把目标类型、标题和链接传入页面脚本。"""
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=make_text_archive())
    target = Target(
        title="测试回答",
        link="https://www.zhihu.com/question/1/answer/2",
        author="someone",
        fetched_at="2026-07-09T12:00:00",
    )
    archiver = Archiver.__new__(Archiver)

    archive = await archiver.extract_text_archive(
        page,
        target,
        TargetType.ANSWER,
        "https://www.zhihu.com/question/1/answer/2",
    )

    evaluate_args = page.evaluate.await_args.args[1]
    assert evaluate_args["targetType"] == TargetType.ANSWER.value
    assert evaluate_args["title"] == "测试回答"
    assert evaluate_args["url"] == "https://www.zhihu.com/question/1/answer/2"
    assert archive["markdown"].startswith("**正文**")


@pytest.mark.asyncio
async def test_extract_text_archive_allows_missing_figcaption() -> None:
    """验证图片没有图注时真实页面脚本不会中断。"""
    html = """
    <html>
      <body>
        <div class="AnswerItem" name="2">
          <meta itemprop="name" content="author">
          <a class="UserLink-link" href="//www.zhihu.com/people/author">author</a>
          <span class="RichText ztext">
            <p><b>body</b></p>
            <figure>
              <img data-original="https://pic.example/no-caption.jpg">
            </figure>
          </span>
        </div>
      </body>
    </html>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "https://www.zhihu.com/question/1/answer/2",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=html,
            ),
        )
        await page.goto("https://www.zhihu.com/question/1/answer/2")
        target = Target(
            title="测试回答",
            link="https://www.zhihu.com/question/1/answer/2",
            author="",
            fetched_at="2026-07-09T12:00:00",
        )
        archiver = Archiver.__new__(Archiver)

        archive = await archiver.extract_text_archive(
            page,
            target,
            TargetType.ANSWER,
            target["link"],
        )

        assert archive["author"] == "author"
        assert "![](https://pic.example/no-caption.jpg)" in archive["markdown"]
        await browser.close()


@pytest.mark.asyncio
async def test_extract_text_archive_does_not_use_broad_post_content() -> None:
    """验证文章正文不会 fallback 到包含推荐区的宽泛容器。"""
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=make_text_archive())
    target = Target(
        title="测试文章",
        link="https://zhuanlan.zhihu.com/p/1",
        author="someone",
        fetched_at="2026-07-09T12:00:00",
    )
    archiver = Archiver.__new__(Archiver)

    await archiver.extract_text_archive(
        page,
        target,
        TargetType.ARTICLE,
        "https://zhuanlan.zhihu.com/p/1",
    )

    script = page.evaluate.await_args.args[0]
    assert 'document.querySelector(".Post-content")' not in script


@pytest.mark.asyncio
async def test_store_one_continues_when_text_archive_write_fails(tmp_path) -> None:
    """验证文本归档写入失败不会中断截图和 info 写入。"""
    archiver = Archiver.__new__(Archiver)
    archiver._base_results_dir = tmp_path
    archiver.people = "someone"
    archiver.save_type = "jpeg"
    archiver.screenshot_max_page_scroll_height = 0
    archiver.referrer_route = AsyncMock()
    archiver.goto = AsyncMock()
    archiver.fill_target_metadata = AsyncMock()
    archiver.prepare_page_for_screenshot = AsyncMock()
    archiver.extract_text_archive = AsyncMock(return_value=make_text_archive())
    archiver.save_text_archive = AsyncMock(side_effect=OSError("磁盘写入失败"))
    archiver.logger = MagicMock()
    page = MagicMock()
    page.route = AsyncMock()
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock(return_value=1000)
    page.screenshot = AsyncMock()
    page.keyboard.press = AsyncMock()
    item = {
        "id": "1234567890abcdef",
        "target": {
            "title": "测试回答",
            "link": "https://www.zhihu.com/question/1/answer/2",
            "author": "someone",
            "fetched_at": datetime(2026, 7, 9, 12, 0, 0),
        },
        "meta": {
            "action": "手动归档",
            "target_type": TargetType.ANSWER,
            "acted_at": datetime(2026, 7, 9, 12, 0, 0),
            "raw": [],
        },
        "people": "someone",
    }

    await archiver.store_one(item, page)

    page.screenshot.assert_awaited_once()
    info_files = list(tmp_path.rglob("info.json"))
    assert len(info_files) == 1
    info = json.loads(info_files[0].read_text(encoding="utf-8"))
    assert info["text_archive"] == {}
