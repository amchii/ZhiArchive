import asyncio
import json
import pathlib
from datetime import datetime, timedelta
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from archive.config import default, settings
from archive.core.base import (
    ActivityItem,
    ActivityMeta,
    BaseWorker,
    Cfg,
    Target,
    TargetType,
    get_correct_target_type,
)
from archive.storage import SQLiteStore
from archive.utils.common import (
    dt_fromisoformat,
    dt_str,
    get_validate_filename,
    uuid_hex,
)
from archive.utils.encoder import JSONEncoder


class Monitor(BaseWorker):
    name = "monitor"
    output_name = "activities"
    archivable_target_types = {TargetType.ANSWER, TargetType.ARTICLE}
    configurable = BaseWorker.configurable + [
        Cfg("save_type"),
    ]

    def __init__(
        self,
        people: str = None,
        init_state_path: str | pathlib.Path = None,
        fetch_until: datetime = datetime.now()
        - timedelta(days=settings.monitor_fetch_until),
        page_default_timeout=30 * 1000,
        interval=60 * 5,
        store: SQLiteStore | None = None,
    ):
        super().__init__(
            people,
            init_state_path,
            page_default_timeout,
            interval=interval,
            store=store,
        )
        self.fetch_until = fetch_until
        self.latest_dt = datetime.now()
        self.save_type: Literal["jpeg", "png"] = "jpeg"

    async def expand_pin_content(self, item_locator: "Locator") -> bool:
        """
        展开想法动态中被折叠的全文内容。

        Args:
            item_locator: 单条知乎动态的定位器。

        Returns:
            无需展开或展开成功时返回 True，发现展开按钮但操作失败时返回 False。
        """
        read_more_locators = item_locator.locator(
            'button.ContentItem-more:has-text("阅读全文"), '
            'a.ContentItem-more:has-text("阅读全文"), '
            'span.ContentItem-more:has-text("阅读全文")'
        )
        try:
            count = await read_more_locators.count()
        except PlaywrightError:
            return False
        if not count:
            return True

        for i in range(count):
            read_more_locator = read_more_locators.nth(i)
            try:
                if not await read_more_locator.is_visible(timeout=500):
                    continue
                await read_more_locator.click(timeout=1000)
                await asyncio.sleep(0.3)
                return True
            except (PlaywrightError, PlaywrightTimeoutError):
                continue
        return False

    async def prepare_item_for_screenshot(
        self,
        item_locator: "Locator",
        target_type: TargetType,
    ) -> None:
        """
        在保存动态卡片截图前处理目标类型相关的页面状态。

        Args:
            item_locator: 单条知乎动态的定位器。
            target_type: 当前动态对应的目标类型。
        """
        if target_type == TargetType.PIN:
            expanded = await self.expand_pin_content(item_locator)
            if not expanded:
                self.logger.warning("想法全文展开失败，将保存当前可见内容。")
        await item_locator.scroll_into_view_if_needed()

    async def extract_pin(self, item_locator: "Locator") -> Target:
        """
        从想法动态卡片中提取用于保存和检索的摘要信息。

        Args:
            item_locator: 单条知乎动态的定位器。
        """
        target_locator = item_locator.locator(default.target_selector)
        author_link_locator = target_locator.locator("a.UserLink-link").first
        now = datetime.now()
        title = await self.extract_pin_title(target_locator)
        try:
            author = await author_link_locator.get_attribute("href", timeout=1000)
        except (PlaywrightError, PlaywrightTimeoutError):
            author = ""

        if author:
            author = author.rsplit("/", maxsplit=1)[-1]
        return {
            "title": title or TargetType.PIN.value,
            "link": "",
            "author": author,
            "fetched_at": now,
        }

    async def extract_pin_title(self, target_locator: "Locator") -> str:
        """
        提取想法正文作为目标标题。

        Args:
            target_locator: 想法动态中的内容定位器。
        """
        try:
            title = await target_locator.locator("span.RichText.ztext").inner_text(
                timeout=1000
            )
        except (PlaywrightError, PlaywrightTimeoutError):
            title = ""
        if title:
            return title.replace("\u200b", "").strip()

        try:
            return await target_locator.evaluate(
                """
                (element) => {
                  const clone = element.cloneNode(true);
                  clone
                    .querySelectorAll(
                      [
                        ".ActivityItem-meta",
                        ".AuthorInfo",
                        ".ContentItem-actions",
                        ".ContentItem-time",
                        "button",
                        "svg",
                      ].join(",")
                    )
                    .forEach((node) => node.remove());
                  return clone.innerText
                    .replace(/阅读全文/g, "")
                    .replace(/\\s+/g, " ")
                    .trim();
                }
                """
            )
        except PlaywrightError:
            return ""

    async def extract_one(
        self,
        item_locator: "Locator",
        target_type: TargetType,
    ) -> Target:
        """
        从动态卡片中提取目标内容信息。

        Args:
            item_locator: 单条知乎动态的定位器。
            target_type: 当前动态对应的目标类型。
        """
        if target_type == TargetType.PIN:
            return await self.extract_pin(item_locator)

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
                await item_locator.locator(
                    "div.ContentItem span.ActivityItem-StickyMark"
                )
                .get_by_text("置顶")
                .count()
            ):
                latest_one_index += 1
                self.logger.warning(f"忽略置顶项：{meta_texts}")
                continue
            action_texts = meta_texts[0]
            acted_at_text = meta_texts[1]
            acted_at = dt_fromisoformat(acted_at_text)
            if i == latest_one_index:
                self.logger.info(f"最新动态时间：{acted_at}")
                self.latest_dt = acted_at
            # 动态时间（e.g. 2023-12-25 16:58)只精确到秒，如果停止时间的那秒有多条动态，则会遗漏
            if acted_at <= until:
                self.logger.info(
                    f"当前动态时间：{acted_at} 早于停止时间：{until}, 将停止本次抓取"
                )
                break
            action_text, target_type_text = action_texts.split("了")
            target_type = get_correct_target_type(action_text, target_type_text)
            if target_type is None:
                self.logger.warning(f"忽略该类型: {action_texts}")
                continue
            await self.prepare_item_for_screenshot(item_locator, target_type)
            target = await self.extract_one(item_locator, target_type)
            self.logger.info(f"于{acted_at_text} {action_texts}\n\t{target['title']}")
            item = ActivityItem(
                id=uuid_hex(),
                target=target,
                meta=ActivityMeta(
                    action=action_text,
                    target_type=target_type,
                    acted_at=acted_at,
                    raw=meta_texts,
                ),
                people=self.people,
            )
            items.append(item)
            filename_suffix = f"-{item['id'][:8]}.{self.save_type}"
            item_filename = get_validate_filename(
                f"{item['meta']['action']}-{item['target']['title']}{filename_suffix}",
                reserved_suffix=filename_suffix,
            )
            target_path = self.get_date_dir(acted_at.date()).joinpath(item_filename)
            await item_locator.screenshot(path=target_path, type=self.save_type)

        return items, count, acted_at

    async def fetch(self, until: datetime, page: Page) -> list[ActivityItem]:
        cur_acted_at = datetime.now()
        start = 0
        items: list[ActivityItem] = []
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
                self.logger.info(
                    f"本次抓取最早动态时间：{cur_acted_at} 早于停止时间：{until}, 将停止"
                )
                break
            self.logger.info("按`End`以触发加载更多")
            await page.keyboard.press("End")
            try:
                await (
                    page.locator(settings.activity_item_selector)
                    .nth(start)
                    .locator("div.ContentItem")
                    .wait_for(
                        timeout=5 * 1000,
                    )
                )
                self.logger.info("加载成功")
            except PlaywrightTimeoutError as e:
                self.logger.info("结束，页面超时")
                self.logger.exception(e)
                await page.screenshot(
                    path=self.results_dir.joinpath(
                        f"error_{cur_acted_at.strftime('%Y%m%d%H%M%S')}.{self.save_type}"
                    ),
                    type=self.save_type,
                    full_page=True,
                )
                break
            i += 1
            await asyncio.sleep(1)
        self.fetch_until = self.latest_dt
        return items

    def filter_archivable_items(
        self,
        items: list["ActivityItem"],
    ) -> list["ActivityItem"]:
        """
        过滤出需要交给 archiver 继续归档的动态。

        Args:
            items: monitor 本次抓取到的动态列表。
        """
        return [
            item
            for item in items
            if item["meta"]["target_type"] in self.archivable_target_types
            and item["target"]["link"]
        ]

    async def save_activity_items(
        self,
        items: list["ActivityItem"],
        filename: str,
    ) -> None:
        """
        将 monitor 抓取到的完整动态结果保存到 activities 目录。

        Args:
            items: monitor 本次抓取到的动态列表。
            filename: 本次结果文件名。
        """
        filepath = self.results_dir.joinpath(filename)
        tmp_filepath = filepath.with_suffix(f"{filepath.suffix}.tmp")
        with open(tmp_filepath, "w", encoding="utf-8") as fp:
            json.dump(items, fp, ensure_ascii=False, indent=2, cls=JSONEncoder)
        tmp_filepath.replace(filepath)
        self.logger.info(f"Save {len(items)} activity items to {filepath}.")

    async def save_archive_task_items(
        self,
        items: list["ActivityItem"],
        filename: str,
    ) -> None:
        """
        将需要深度归档的动态保存为 archiver 任务并推送队列。

        Args:
            items: 需要交给 archiver 的动态列表。
            filename: 本次任务文件名。
        """
        inserted = await self.sqlite_store.enqueue_monitor_items_and_checkpoint(
            self.people,
            items,
            self.fetch_until,
            self.latest_dt,
        )
        self.logger.info(
            f"Save {len(items)} archive task items to SQLite, inserted {inserted}."
        )
        if inserted and hasattr(self, "archive_event"):
            self.archive_event.set()

    async def save_and_push(self, items: list["ActivityItem"]) -> None:
        """
        保存 monitor 结果，并只把回答/文章推送给 archiver。

        Args:
            items: monitor 本次抓取到的动态列表。
        """
        if not items:
            await self.save_archive_task_items([], "")
            self.logger.info("No items, only checkpoint updated.")
            return
        filename = f"{dt_str()}.json"
        await self.save_activity_items(items, filename)
        archive_items = self.filter_archivable_items(items)
        await self.save_archive_task_items(archive_items, filename)
        if not archive_items:
            self.logger.info("No archive task items, only checkpoint updated.")

    async def before_run(self):
        """
        运行前加载 SQLite 配置和当前用户的抓取检查点。
        """
        await super().before_run()
        checkpoint = await self.sqlite_store.get_monitor_checkpoint(self.people)
        self.fetch_until = checkpoint["fetch_until"]
        if checkpoint["latest_dt"] is not None:
            self.latest_dt = checkpoint["latest_dt"]

    async def _run(self, playwright, headless=True, **context_extra):
        self.logger.info("Starting a new fetch loop...")
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            page = await self.new_page(context)
            await self.goto(page, self.person_page_url)
            await asyncio.sleep(1)
            results = await self.fetch(self.fetch_until, page)
            await self.save_and_push(results)
            self.logger.info("Done, wait for next fetch loop")
            return results
