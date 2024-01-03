import asyncio
import json
import os
import pathlib
from datetime import datetime, timedelta

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from archive.config import default, settings
from archive.core.base import (
    ActivityItem,
    ArchiveTask,
    Base,
    Cfg,
    Target,
    get_correct_target_type,
)
from archive.utils.common import (
    dt_fromisoformat,
    dt_str,
    dt_toisoformat,
    get_validate_filename,
    uuid_hex,
)
from archive.utils.encoder import JSONEncoder


class Monitor(Base):
    name = "monitor"
    configurable = Base.configurable + [
        Cfg("fetch_until", dt_toisoformat, dt_fromisoformat, read_only=True)
    ]

    def __init__(
        self,
        people: str = None,
        init_state_path: str | pathlib.Path = None,
        fetch_until: datetime = datetime.now() - timedelta(days=10),
        person_page_url=None,
        page_default_timeout=10 * 1000,
        interval=60 * 5,
    ):
        super().__init__(
            people,
            init_state_path,
            person_page_url,
            page_default_timeout,
            interval=interval,
        )
        self.fetch_until = fetch_until
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
        self, until: datetime, page: Page, start: int = 0, acted_at=None
    ) -> tuple[list["ActivityItem"], int, datetime]:
        items_locator = page.locator(settings.activity_item_selector)
        items: list[ActivityItem] = []
        count = 0
        total = await items_locator.count()
        self.logger.info(f"本次动态起点: {start}, 共{total}条")
        self.logger.info(
            f"抓取停止时间: {until}, 起点动态时间: {acted_at or '无'}",
        )
        acted_at = acted_at or datetime.now()
        latest_one_index = 0
        for i in range(start, total):
            self.logger.info(f"动态序号: {i}")
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
                latest_one_index += 1
                self.logger.warning("忽略置顶")
                continue
            action_texts = meta_texts[0]
            acted_at_text = meta_texts[1]
            acted_at = dt_fromisoformat(acted_at_text)
            if i == latest_one_index:
                self.logger.info(f"最新动态时间：{acted_at}")
                self.latest_dt = acted_at
            # 动态时间（e.g. 2023-12-25 16:58)只精确到秒，如果停止时间的那秒有多条动态，则会遗漏
            if acted_at <= until:
                self.logger.info(f"当前动态时间：{acted_at} 早于停止时间：{until}, 将停止本次抓取")
                break
            action_text, target_type_text = action_texts.split("了")
            target_type = get_correct_target_type(action_text, target_type_text)
            if target_type is None:
                self.logger.warning(f"忽略该类型: {action_texts}")
                continue
            target = await self.extract_one(item_locator)
            self.logger.info(f"于{acted_at_text} {action_texts}\n\t{target['title']}")
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
        self.logger.info("按动态页从上至下（从新向旧）抓取...")
        while cur_acted_at > until:
            self.logger.info(f"第{i}次抓取")
            _items, count, cur_acted_at = await self.fetch_once(
                until, page, start, cur_acted_at
            )
            start += count
            items.extend(_items)
            if cur_acted_at <= until:
                self.logger.info(f"本次抓取最早动态时间：{cur_acted_at} 早于停止时间：{until}, 将停止")
                break
            self.logger.info("Press End.")
            await page.keyboard.press("End")
            try:
                await page.locator(settings.activity_item_selector).nth(start).locator(
                    "div.ContentItem"
                ).wait_for(
                    timeout=5 * 1000,
                )
                self.logger.info("Load success")
            except PlaywrightTimeoutError as e:
                self.logger.info("Done, due to timeout")
                self.logger.exception(e)
                await page.screenshot(
                    path=self.results_dir.joinpath(
                        f"error_{cur_acted_at.strftime('%Y%m%d%H%M%S')}.png"
                    ),
                    type="png",
                    full_page=True,
                )
                break
            i += 1
            await asyncio.sleep(1)
        self.fetch_until = self.latest_dt
        return items

    async def save_and_push(self, items: list["ActivityItem"]):
        if not items:
            self.logger.info("No items, will do nothing.")
            return
        filename = f"{dt_str()}.json"
        filepath = self.activity_dir.joinpath(filename)
        with open(filepath, "w") as fp:
            json.dump(items, fp, ensure_ascii=False, indent=2, cls=JSONEncoder)
            self.logger.info(f"Save {len(items)} items to {filepath}.")
        task = ArchiveTask(filepath)
        await self.push_task(task)
        self.logger.info(f"Push a task {task} to task list")

    async def _run(self, playwright, headless=True, **context_extra):
        self.logger.info("Starting a new fetch loop...")
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            page = await self.new_page(context)
            page.set_default_timeout(self.page_default_timeout)
            await self.goto(page, self.person_page_url)
            results = await self.fetch(self.fetch_until, page)
            await self.save_and_push(results)
            self.logger.info("Done, wait for next fetch loop")
            return results
