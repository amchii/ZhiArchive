import asyncio
import contextlib
import json
import os
import pathlib
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Coroutine, TypedDict
from urllib import parse

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Response,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from archive.config import default, settings
from archive.utils import js
from archive.utils.encoder import JSONEncoder


class AbnormalError(Exception):
    pass


class Action(str, Enum):
    AGREE = "赞同"
    ANSWER = "回答"
    POST_ARTICLE = "发表"
    POST_PIN = "发布"
    # 其他的不关心


class TargetType(str, Enum):
    ANSWER = "回答"
    ARTICLE = "文章"
    # 其他的不关心
    PIN = "想法"


class Target(TypedDict):
    title: str
    link: str
    author: str
    fetched_at: datetime


class ActivityMeta(TypedDict):
    action: str
    target_type: str
    acted_at: datetime
    raw: list["str"] | None


class ActivityItem(TypedDict):
    meta: ActivityMeta
    target: Target


def now_str():
    return datetime.now().strftime("%Y%m%d%H%M%S")


def get_correct_target_type(action_text, target_type_text) -> TargetType | None:
    try:
        action = Action(action_text)
        if action == Action.AGREE:
            return TargetType(target_type_text)
        elif action == Action.ANSWER:
            return TargetType.ANSWER
        elif action == Action.POST_ARTICLE and target_type_text == TargetType.ARTICLE:
            return TargetType.ARTICLE
        return
    except ValueError:
        return


def abort_with(error_code: str = None):
    async def abort(route: Route):
        await route.abort(error_code)

    return abort


async def init_context(context: BrowserContext):
    context.set_default_timeout(settings.context_default_timeout)
    await context.add_init_script(js.set_webdriver_js_script)
    return context


@contextlib.asynccontextmanager
async def get_context(
    playwright: Playwright,
    state_path: str | pathlib.Path,
    browser_headless=True,
    init: Callable[[BrowserContext], Coroutine[Any, Any, BrowserContext]]
    | None = init_context,
    locale="zh-CN",
    **extra,
) -> BrowserContext:
    browser = await playwright.chromium.launch(headless=browser_headless)
    context = await browser.new_context(
        storage_state=state_path, locale=locale, **extra
    )
    if init:
        await init(context)
    try:
        yield context
    finally:
        await context.storage_state(path=state_path)


class Base:
    abnormal_texts = ["您的网络环境存在异常", "请输入验证码进行验证", "意见反馈"]

    def __init__(
        self,
        people: str,
        state_path: str | pathlib.Path,
        page_default_timeout: int = 10 * 1000,
        results_dir: str | pathlib.Path = None,
    ):
        self.people = people
        self.state_path = state_path
        self.page_default_timeout = page_default_timeout
        self._results_dir = results_dir

    @property
    def results_dir(self):
        if not self._results_dir:
            self._results_dir = settings.results_dir.joinpath(f"{self.people}")
        os.makedirs(self._results_dir, exist_ok=True)
        return self._results_dir

    @classmethod
    def batch_url_match(cls, url: str) -> bool:
        if "zhihu-web-analytics.zhihu.com" in url:
            return True
        return False

    async def new_page(self, context: BrowserContext) -> Page:
        page = await context.new_page()
        page.set_default_timeout(self.page_default_timeout)
        return page

    async def init_context(self, context: BrowserContext) -> BrowserContext:
        await init_context(context)
        await context.route(self.batch_url_match, abort_with("failed"))
        return context

    @contextlib.asynccontextmanager
    async def get_context(
        self, playwright: Playwright, browser_headless=True, **context_extra
    ) -> BrowserContext:
        async with get_context(
            playwright,
            self.state_path,
            browser_headless,
            init=self.init_context,
            **context_extra,
        ) as context:
            yield context

    async def goto(self, page: Page, url, **kwargs):
        response = await page.goto(url, **kwargs)
        if await self.is_abnormal(response):
            dt = datetime.now().strftime("%Y%m%d%H%M%S")
            await page.screenshot(path=f"异常{dt}.png", full_page=True)
            raise AbnormalError(f"{url}: \n{await response.text()}")
        return response

    async def is_abnormal(self, response: Response) -> bool:
        text = await response.text()
        if all([abnormal_text in text for abnormal_text in self.abnormal_texts]):
            return True
        return False


class ActivityMonitor(Base):
    def __init__(
        self,
        people: str,
        state_path,
        fetch_until=datetime.now() - timedelta(days=10),
        person_page_url=None,
        page_default_timeout=10 * 1000,
    ):
        super().__init__(people, state_path, page_default_timeout)
        self.fetch_until = fetch_until
        self.person_page_url = person_page_url or default.person_page_url.format(
            people=people
        )

    async def extract_one(
        self,
        item_locator: "Locator",
    ) -> Target:
        target_locator = item_locator.locator(default.target_selector)
        target_link_locator = target_locator.locator("h2 a[target=_blank]")
        content_item_meta_locator = target_locator.locator("div.ContentItem-meta")
        author_link_locator = content_item_meta_locator.locator(
            "div.AuthorInfo div.AuthorInfo-content span.UserLink a.UserLink-link"
        )
        now = datetime.now()
        try:
            title: str = await target_link_locator.text_content(timeout=1 * 1000)
            link: str = await target_link_locator.get_attribute("href")
            author: str = await author_link_locator.get_attribute("href")
        except PlaywrightTimeoutError:
            return {"title": "", "link": "", "author": "", "fetched_at": now}
        author = author.rsplit("/", maxsplit=1)[-1]
        return {"title": title, "link": link, "author": author, "fetched_at": now}

    async def fetch_once(
        self, until: datetime, page: Page, start: int = 0, acted_at=datetime.now()
    ) -> tuple[list["ActivityItem"], int, datetime]:
        items_locator = page.locator(settings.activity_item_selector)
        items: list[ActivityItem] = []
        count = 0
        total = await items_locator.count()
        print("start: ", start, "total: ", total)
        for i in range(start, total):
            print(i)
            item_locator = items_locator.nth(i)
            meta_locator = item_locator.locator("div.ActivityItem-meta")
            meta_texts = await meta_locator.locator("span").all_text_contents()
            print("meta: ", meta_texts)
            if len(meta_texts) < 2:
                continue
            count += 1
            # 忽略置顶
            if (
                await item_locator.locator("div.ContentItem h2.ContentItem-title span")
                .get_by_text("置顶")
                .count()
            ):
                print("忽略置顶")
                continue
            action_texts = meta_texts[0]
            action_text, target_type_text = action_texts.split("了")
            target_type = get_correct_target_type(action_text, target_type_text)
            if target_type is None:
                print(f"忽略该类型: {action_texts}")
                continue
            acted_at_text = meta_texts[1]
            acted_at = datetime.fromisoformat(acted_at_text)
            target = await self.extract_one(item_locator)
            items.append(
                {
                    "meta": {
                        "action": action_text,
                        "target_type": target_type.value,
                        "acted_at": acted_at,
                        "raw": meta_texts,
                    },
                    "target": target,
                }
            )
            item_filename = f"{action_text}-{target['title']}.png"
            await item_locator.screenshot(
                path=self.results_dir.joinpath(item_filename), type="png"
            )
            if acted_at < until:
                break
        return items, count, acted_at

    async def fetch(self, until: datetime, page: Page) -> list["ActivityItem"]:
        cur_acted_at = datetime.now()
        start = 0
        items = []
        i = 1
        while cur_acted_at >= until:
            print(f"第{i}次fetch")
            _items, count, cur_acted_at = await self.fetch_once(
                until, page, start, cur_acted_at
            )
            start += count
            items.extend(_items)
            await page.keyboard.press("End")
            try:
                await page.locator(settings.activity_item_selector).nth(start).locator(
                    "div.ContentItem"
                ).wait_for(
                    timeout=5 * 1000,
                )
                print("Load success")
            except PlaywrightTimeoutError:
                print("Done, due to timeout")
                break
            i += 1
            await asyncio.sleep(0.5)
        return items

    async def _run(self, playwright, headless=True, **context_extra):
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            page = await self.new_page(context)
            page.set_default_timeout(self.page_default_timeout)
            await self.goto(page, self.person_page_url)
            results = await self.fetch(self.fetch_until, page)
            filename = f"{now_str()}.json"
            with open(self.results_dir.joinpath(filename), "w") as fp:
                json.dump(results, fp, ensure_ascii=False, indent=2, cls=JSONEncoder)
            await page.pause()
        return results

    async def run(self, headless=True, **context_extra):
        async with async_playwright() as playwright:
            return await self._run(playwright, headless, **context_extra)


class Archiver(Base):
    async def store_one(self, item: ActivityItem, context: BrowserContext):
        # 每个对象都新开一个标签页
        page = await self.new_page(context)
        target = item["target"]
        if not target["link"]:
            return
        r = parse.urlparse(target["link"])
        url = "https://" + "".join(r[1:])
        await self.goto(page, url)
        await page.wait_for_timeout(timeout=100)
        filename = f"{item['meta']['action']}-{target['title']}.png"
        await page.screenshot(
            path=self.results_dir.joinpath(filename), type="png", full_page=True
        )
        await page.close()

    async def store(
        self,
        playwright,
        items_list: list["ActivityItem"],
        headless=True,
        **context_extra,
    ):
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            empty_page = await self.new_page(context)
            for item in items_list:
                await self.store_one(item, context)
                await asyncio.sleep(0.3)
            await empty_page.close()

    async def run(
        self,
        items_list: list["ActivityItem"],
        headless=True,
        **context_extra,
    ):
        async with async_playwright() as playwright:
            return await self.store(playwright, items_list, headless, **context_extra)
