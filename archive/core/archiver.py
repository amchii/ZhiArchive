import asyncio
import json
import re
from datetime import datetime
from typing import Literal
from urllib import parse

import aiofiles
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route

from archive.config import settings
from archive.core.base import (
    ActivityItem,
    ActivityMeta,
    ArchiveTask,
    BaseWorker,
    Cfg,
    Target,
    TargetType,
)
from archive.utils.common import (
    dt_fromisoformat,
    dt_str,
    get_validate_filename,
    uuid_hex,
)
from archive.utils.encoder import JSONEncoder
from archive.utils.js import get_page_scrollHeight, get_page_scrollWidth

ANSWER_PATH_PATTERN = re.compile(r"^/question/\d+/answer/\d+/?$")
ARTICLE_PATH_PATTERN = re.compile(r"^/p/\d+/?$")


def parse_archive_url(url: str) -> tuple[str, TargetType]:
    """
    校验并标准化可由 archiver 保存的知乎链接。

    Args:
        url: 知乎回答或专栏文章链接。

    Returns:
        标准化后的 HTTPS 链接及目标类型。

    Raises:
        ValueError: 链接不是受支持的知乎回答或专栏文章。
    """
    value = url.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("/question/"):
        value = f"https://www.zhihu.com{value}"

    parsed = parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("请输入完整的知乎回答或文章链接")
    if parsed.username or parsed.password:
        raise ValueError("链接中不能包含用户认证信息")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("链接端口格式不正确") from error
    if port not in {None, 80, 443}:
        raise ValueError("链接不能包含自定义端口")

    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    if host in {"zhihu.com", "www.zhihu.com"} and ANSWER_PATH_PATTERN.fullmatch(
        parsed.path
    ):
        target_type = TargetType.ANSWER
        host = "www.zhihu.com"
    elif host == "zhuanlan.zhihu.com" and ARTICLE_PATH_PATTERN.fullmatch(parsed.path):
        target_type = TargetType.ARTICLE
    else:
        raise ValueError("仅支持知乎回答或专栏文章链接")

    return parse.urlunparse(("https", host, path, "", "", "")), target_type


class Archiver(BaseWorker):
    name = "archiver"
    output_name = "archives"
    configurable = BaseWorker.configurable + [
        Cfg(
            "screenshot_max_page_scroll_height",
        ),
        Cfg("save_type"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.screenshot_max_page_scroll_height = (
            settings.screenshot_max_page_scroll_height
        )
        self.save_type: Literal["jpeg", "png"] = "jpeg"

    async def referrer_route(self, route: Route):
        headers = route.request.headers
        headers["Referer"] = self.person_page_url
        await route.continue_(headers=headers)

    async def prepare_page_for_screenshot(self, page: Page) -> None:
        """
        清理知乎页面中会干扰长截图拼接的浮动元素。
        """
        await page.evaluate(
            """
            () => {
              const hideElement = (element) => {
                if (!element) {
                  return;
                }
                element.setAttribute("data-zhi-archive-hidden", "true");
                element.style.setProperty("display", "none", "important");
              };

              const findFixedAncestor = (element) => {
                let current = element;
                while (current && current !== document.body) {
                  const style = window.getComputedStyle(current);
                  if (style.position === "fixed") {
                    return current;
                  }
                  current = current.parentElement;
                }
                return null;
              };

              const appHeader = document.querySelector(".AppHeader");
              hideElement(findFixedAncestor(appHeader));

              document
                .querySelectorAll(".ContentItem-actions.is-fixed, .CornerButtons")
                .forEach(hideElement);
            }
            """
        )
        await page.wait_for_timeout(timeout=200)

    async def enqueue_url(self, url: str) -> tuple[ArchiveTask, ActivityItem]:
        """
        将一个知乎回答或文章链接加入归档队列。

        Args:
            url: 知乎回答或专栏文章链接。

        Returns:
            已推送的任务及其动态数据。
        """
        normalized_url, target_type = parse_archive_url(url)
        await self.global_configurator.load_to_worker(sync=False)
        now = datetime.now()
        item_id = uuid_hex()
        item = ActivityItem(
            id=item_id,
            target=Target(
                title="",
                link=normalized_url,
                author="",
                fetched_at=now,
            ),
            meta=ActivityMeta(
                action="手动归档",
                target_type=target_type,
                acted_at=now,
                raw=[normalized_url],
            ),
            people=self.people,
        )
        filepath = self.tasks_dir.joinpath(f"manual-{dt_str(now)}-{item_id[:8]}.json")
        async with aiofiles.open(filepath, "w", encoding="utf-8") as fp:
            await fp.write(
                json.dumps([item], ensure_ascii=False, indent=2, cls=JSONEncoder)
            )
        task = ArchiveTask(filepath)
        await self.push_task(task)
        self.logger.info(f"Push a manual archive task {task} to task list")
        return task, item

    async def fill_target_metadata(
        self,
        page: Page,
        target: Target,
        target_type: TargetType,
    ) -> None:
        """
        为手动归档任务补全页面标题和作者。

        Args:
            page: 已打开目标页面的 Playwright 页面。
            target: 待补全的目标数据。
            target_type: 回答或文章类型。
        """
        if not target["title"]:
            try:
                title = await page.locator('meta[property="og:title"]').get_attribute(
                    "content", timeout=1000
                )
            except PlaywrightError:
                title = ""
            if not title:
                title = await page.title()
            target["title"] = title.removesuffix(" - 知乎").strip()

        if target["author"]:
            return
        if target_type == TargetType.ANSWER:
            author_selector = "div.AnswerCard a.UserLink-link"
        else:
            author_selector = (
                "div.Post-Author a.UserLink-link, div.AuthorInfo a.UserLink-link"
            )
        try:
            author_href = await page.locator(author_selector).first.get_attribute(
                "href", timeout=1000
            )
        except PlaywrightError:
            author_href = ""
        if author_href:
            target["author"] = author_href.rstrip("/").rsplit("/", maxsplit=1)[-1]

    async def store_one(self, item: ActivityItem, page: Page) -> Page | None:
        """
        打开并保存一个回答或文章归档。

        Args:
            item: 待归档的动态数据。
            page: 用于访问目标链接的 Playwright 页面。
        """
        target = item["target"]
        meta = item["meta"]
        if not target["link"]:
            return
        url, target_type = parse_archive_url(target["link"])
        await page.route(url, self.referrer_route)
        await self.goto(page, url)
        await self.fill_target_metadata(page, target, target_type)
        if not target["title"]:
            target["title"] = f"{target_type.value}-{item['id'][:8]}"
        # 确保页面中的图片被加载
        if target_type == TargetType.ANSWER:
            imgs_locator = page.locator("div.AnswerCard figure img")
        else:
            imgs_locator = page.locator("div.Post-RichTextContainer figure img")
        for i in range(await imgs_locator.count()):
            img_locator = imgs_locator.nth(i)
            await img_locator.scroll_into_view_if_needed()
        await page.wait_for_timeout(timeout=500)
        await self.prepare_page_for_screenshot(page)

        now = datetime.now()
        acted_at = dt_fromisoformat(meta["acted_at"])
        title_suffix = f"-{item['id'][:8]}"
        title = get_validate_filename(
            f"{item['meta']['action']}-{item['target']['title']}{title_suffix}",
            reserved_suffix=title_suffix,
        )
        # todo: 或许直接通过`ActivityItem.people`来确定保存地址更合理
        target_dir = self.get_date_dir(acted_at.date()).joinpath(title)
        screenshot_path = target_dir.joinpath(f"{title}.{self.save_type}")
        page_scroll_height = await page.evaluate(get_page_scrollHeight)
        if 0 < self.screenshot_max_page_scroll_height < page_scroll_height:
            page_scroll_width = await page.evaluate(get_page_scrollWidth)
            clip = {
                "x": 0,
                "y": 0,
                "width": page_scroll_width,
                "height": self.screenshot_max_page_scroll_height,
            }
            self.logger.warning(
                f"Page's scrollHeight({page_scroll_height}) is greater than `screenshot_max_page_scroll_height`({self.screenshot_max_page_scroll_height})."
            )
        else:
            clip = None
        self.logger.info(f"Saving screenshot to {screenshot_path}.")
        await page.screenshot(
            path=screenshot_path, type=self.save_type, full_page=True, clip=clip
        )
        info = {
            "title": target["title"],
            "url": url,
            "author": target["author"],
            "shot_at": now,
        }
        info_path = target_dir.joinpath("info.json")
        async with aiofiles.open(info_path, "w", encoding="utf-8") as fp:
            await fp.write(
                json.dumps(info, ensure_ascii=False, indent=2, cls=JSONEncoder)
            )
        await page.keyboard.press("PageDown")
        await asyncio.sleep(0.5)
        await page.keyboard.press("PageDown")
        return page

    async def store(
        self,
        playwright,
        item_list: list["ActivityItem"],
        headless=True,
        **context_extra,
    ):
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            empty_page = await self.new_page(context)
            self.logger.info(f"Will fetch {len(item_list)} items")
            for item in item_list:
                # 每个对象都新开一个标签页
                page = await self.new_page(context)
                try:
                    await self.store_one(item, page=page)
                except PlaywrightError as e:
                    self.logger.warning(e)
                finally:
                    await page.close()
                await asyncio.sleep(1)
            self.logger.info("Fetch done")
            await empty_page.close()

    async def _run(self, playwright, headless=True, **context_extra):
        if task := await self.pop_task():
            self.logger.info(f"New archive task: {task}")
            async with aiofiles.open(task.activity_path, encoding="utf-8") as fp:
                item_list = json.loads(await fp.read())
            await self.store(playwright, item_list, headless, **context_extra)
