import asyncio
import contextlib
import json
import logging
import os
import pathlib
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Callable, Coroutine, TypedDict
from urllib import parse

import aiofiles
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
from redis import asyncio as aioredis

from archive.config import default, settings
from archive.utils import js
from archive.utils.common import (
    dt_fromisoformat,
    dt_str,
    get_validate_filename,
    uuid_hex,
)
from archive.utils.encoder import JSONEncoder

logger = logging.getLogger("archive")


class AbnormalError(Exception):
    pass


class Action(str, Enum):
    AGREE = "赞同"
    ANSWER = "回答"
    POST_ARTICLE = "发表"
    POST_PIN = "发布"
    COLLECT = "收藏"
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
    fetched_at: datetime | str


class ActivityMeta(TypedDict):
    action: str
    target_type: str
    acted_at: datetime | str
    raw: list["str"] | None


class ActivityItem(TypedDict):
    id: str
    meta: ActivityMeta
    target: Target


def get_correct_target_type(action_text, target_type_text) -> TargetType | None:
    try:
        action = Action(action_text)
        if action in (Action.AGREE, Action.COLLECT):
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
    state_auto_save: bool = True,
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
        if state_auto_save:
            await context.storage_state(path=state_path)
        await context.close()
        await browser.close()


class ArchiveTask:
    def __init__(self, activity_info_path):
        self.activity_path = pathlib.Path(activity_info_path).resolve()

    @property
    def task_name(self):
        return str(self.activity_path)

    def as_value(self) -> str:
        return f"{self.activity_path}"

    @classmethod
    def from_value(cls, v: str) -> "ArchiveTask":
        return cls(v)

    def __str__(self):
        return f"{self.__class__.__name__}<{self.as_value()}>"

    __repr__ = __str__


class Base:
    name = ""
    redis_key_prefix = "zhi_archive:archive"
    state_path_key = f"{redis_key_prefix}:state_path"
    tasks_key = f"{redis_key_prefix}:tasks"  # list
    tasks_result_key = f"{redis_key_prefix}:task_results"  # hash
    abnormal_texts = ["您的网络环境存在异常", "请输入验证码进行验证", "意见反馈"]

    def __init__(
        self,
        people: str,
        init_state_path: str | pathlib.Path,
        page_default_timeout: int = 20 * 1000,
        results_dir: str | pathlib.Path = None,
        redis_url: str = settings.redis_url,
        interval: int = 10,
    ):
        self.people = people
        self.init_state_path = init_state_path
        self.page_default_timeout = page_default_timeout
        self._results_dir = results_dir
        self.redis = aioredis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
        self.interval = interval

    @property
    def personal_key(self):
        return f"{self.redis_key_prefix}:{self.people}"

    async def get_state_path_from_redis(self) -> pathlib.Path | None:
        path = await self.redis.get(self.state_path_key)
        return pathlib.Path(path) if path else None

    async def set_state_path_to_redis(self, path: str | pathlib.Path):
        await self.redis.set(self.state_path_key, str(path))

    async def get_state_path(self) -> pathlib.Path | str:
        return await self.get_state_path_from_redis() or self.init_state_path

    async def push_task(self, task: ArchiveTask):
        return await self.redis.rpush(self.tasks_key, task.as_value())

    async def pop_task(self) -> ArchiveTask | None:
        task = await self.redis.lpop(self.tasks_key)
        if task:
            return ArchiveTask.from_value(task)

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
        return context

    @contextlib.asynccontextmanager
    async def get_context(
        self,
        playwright: Playwright,
        state_auto_save: bool = True,
        browser_headless=True,
        **context_extra,
    ) -> BrowserContext:
        state_path = await self.get_state_path()
        logger.info(f"Currently used state path: {state_path}")
        async with get_context(
            playwright,
            state_path,
            state_auto_save,
            browser_headless,
            init=self.init_context,
            **context_extra,
        ) as context:
            yield context

    async def goto(self, page: Page, url, **kwargs):
        response = await page.goto(url, **kwargs)
        if await self.is_abnormal(response):
            await page.screenshot(
                path=settings.results_dir.joinpath(f"异常{dt_str()}.png"), full_page=True
            )
            raise AbnormalError(f"{url}: \n{await response.text()}")
        return response

    async def is_abnormal(self, response: Response) -> bool:
        text = await response.text()
        if all([abnormal_text in text for abnormal_text in self.abnormal_texts]):
            return True
        return False

    async def handle_abnormal(self, *args, **kwargs):
        pass

    @property
    def pause_key(self):
        return f"{self.redis_key_prefix}:{self.name}:pause"

    async def pause(self):
        return await self.redis.set(self.pause_key, 1)

    async def resume(self):
        return await self.redis.set(self.pause_key, 0)

    async def need_pause(self) -> bool:
        return int(await self.redis.get(self.pause_key) or 0) == 1

    async def _run(self, playwright, headless=True, **context_extra):
        raise NotImplementedError

    async def run(
        self,
        headless=True,
        **context_extra,
    ):
        logger.info(f"{self.name} started.")
        async with async_playwright() as playwright:
            while True:
                if await self.need_pause():
                    logger.info(f"{self.name} pausing")
                    while await self.need_pause():
                        await asyncio.sleep(1)
                    logger.info(f"{self.name} resumed")
                try:
                    await self._run(playwright, headless, **context_extra)
                except AbnormalError as e:
                    logger.error(e)
                    await self.handle_abnormal()
                except Exception as e:
                    logger.exception(e)
                await asyncio.sleep(self.interval)


class APIClient(Base):
    def __init__(self, as_: str = None):
        self.name = as_ or self.name
        super().__init__(
            settings.people, settings.states_dir.joinpath(default.state_file)
        )


class ActivityMonitor(Base):
    name = "monitor"

    def __init__(
        self,
        people: str,
        init_state_path,
        fetch_until=datetime.now() - timedelta(days=10),
        person_page_url=None,
        page_default_timeout=10 * 1000,
        interval=60 * 5,
    ):
        super().__init__(
            people, init_state_path, page_default_timeout, interval=interval
        )
        self.fetch_until = fetch_until
        self.person_page_url = person_page_url or default.person_page_url.format(
            people=people
        )
        self.latest_dt = datetime.now()

    @property
    def activity_dir(self):
        d = self.results_dir.joinpath("activities")
        os.makedirs(d, exist_ok=True)
        return d

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
        logger.info(f"start: {start}, total: {total}")
        logger.info(
            f"until: {until}, cur_acted_at: {acted_at}",
        )
        for i in range(start, total):
            logger.info(i)
            item_locator = items_locator.nth(i)
            meta_locator = item_locator.locator("div.ActivityItem-meta")
            meta_texts = await meta_locator.locator("span").all_text_contents()
            if len(meta_texts) < 2:
                continue
            count += 1
            # 忽略置顶
            if (
                await item_locator.locator("div.ContentItem h2.ContentItem-title span")
                .get_by_text("置顶")
                .count()
            ):
                logger.warning("忽略置顶")
                continue
            action_texts = meta_texts[0]
            action_text, target_type_text = action_texts.split("了")
            target_type = get_correct_target_type(action_text, target_type_text)
            if target_type is None:
                logger.warning(f"忽略该类型: {action_texts}")
                continue
            acted_at_text = meta_texts[1]
            acted_at = dt_fromisoformat(acted_at_text)
            if start == 0:
                logger.info(f"最新动态时间：{acted_at}")
                self.latest_dt = acted_at
            if acted_at < until:
                logger.info(f"当前动态时间：{acted_at} 早于停止时间：{until}, 将停止本次抓取")
                break
            target = await self.extract_one(item_locator)
            items.append(
                {
                    "id": uuid_hex(),
                    "meta": {
                        "action": action_text,
                        "target_type": target_type.value,
                        "acted_at": acted_at,
                        "raw": meta_texts,
                    },
                    "target": target,
                }
            )
            item_filename = get_validate_filename(
                f"{action_text}-{target['title']}.png"
            )
            await item_locator.screenshot(
                path=self.activity_dir.joinpath(item_filename), type="png"
            )

        return items, count, acted_at

    async def fetch(self, until: datetime, page: Page) -> list["ActivityItem"]:
        cur_acted_at = datetime.now()
        start = 0
        items = []
        i = 1
        while cur_acted_at >= until:
            logger.info(f"第{i}次fetch")
            _items, count, cur_acted_at = await self.fetch_once(
                until, page, start, cur_acted_at
            )
            start += count
            items.extend(_items)
            if cur_acted_at < until:
                logger.info(f"本次fetch最早动态时间：{cur_acted_at} 早于停止时间：{until}, 将停止")
                break
            await page.keyboard.press("End")
            try:
                await page.locator(settings.activity_item_selector).nth(start).locator(
                    "div.ContentItem"
                ).wait_for(
                    timeout=5 * 1000,
                )
                logger.info("Load success")
            except PlaywrightTimeoutError:
                logger.info("Done, due to timeout")
                break
            i += 1
            await asyncio.sleep(0.5)
        return items

    async def _run(self, playwright, headless=True, **context_extra):
        logger.info("Starting a new fetch loop...")
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            page = await self.new_page(context)
            page.set_default_timeout(self.page_default_timeout)
            await self.goto(page, self.person_page_url)
            results = await self.fetch(self.fetch_until, page)
            filename = f"{dt_str()}.json"
            filepath = self.activity_dir.joinpath(filename)
            with open(filepath, "w") as fp:
                json.dump(results, fp, ensure_ascii=False, indent=2, cls=JSONEncoder)
            self.fetch_until = self.latest_dt
            if results:
                logger.info("Push a task to task list")
                await self.push_task(ArchiveTask(filepath))
            logger.info("Done, wait for next fetch loop")
            return results


class Archiver(Base):
    name = "archiver"

    @property
    def archive_dir(self):
        return self.results_dir.joinpath("archive")

    def get_date_dir(self, dt: date) -> pathlib.Path:
        date_dir = self.archive_dir.joinpath(dt.strftime("%Y/%m/%d"))
        os.makedirs(date_dir, exist_ok=True)
        return date_dir

    async def store_one(self, item: ActivityItem, context: BrowserContext):
        # 每个对象都新开一个标签页
        target = item["target"]
        meta = item["meta"]
        if not target["link"]:
            return
        r = parse.urlparse(target["link"])
        url = "https://" + "".join(r[1:])
        page = await self.new_page(context)
        await self.goto(page, url)
        await page.wait_for_timeout(timeout=100)

        now = datetime.now()
        acted_at = dt_fromisoformat(meta["acted_at"])
        title = get_validate_filename(f"{item['meta']['action']}-{target['title']}")
        target_dir = self.get_date_dir(acted_at.date()).joinpath(title)
        screenshot_path = target_dir.joinpath(f"{title}.png")
        await page.screenshot(path=screenshot_path, type="png", full_page=True)
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
            for item in item_list:
                await self.store_one(item, context)
                await asyncio.sleep(0.3)
            await empty_page.close()

    async def _run(self, playwright, headless=True, **context_extra):
        if task := await self.pop_task():
            logger.info(f"new archive task: {task}")
            async with aiofiles.open(task.activity_path, encoding="utf-8") as fp:
                item_list = json.loads(await fp.read())
            await self.store(playwright, item_list, headless, **context_extra)
