import asyncio
import json
from datetime import datetime
from urllib import parse

import aiofiles
from playwright.async_api import BrowserContext, Route

from archive.core.base import ActivityItem, Base, TargetType
from archive.utils.common import dt_fromisoformat, get_validate_filename
from archive.utils.encoder import JSONEncoder


class Archiver(Base):
    name = "archiver"
    output_name = "archives"

    async def referrer_route(self, route: Route):
        headers = route.request.headers
        headers["Referer"] = self.person_page_url
        await route.continue_(headers=headers)

    async def store_one(self, item: ActivityItem, context: BrowserContext):
        # 每个对象都新开一个标签页
        target = item["target"]
        meta = item["meta"]
        if not target["link"]:
            return
        r = parse.urlparse(target["link"])
        url = "https://" + "".join(r[1:])
        page = await self.new_page(context)
        await page.route(url, self.referrer_route)
        await self.goto(page, url)
        if meta["target_type"] == TargetType.ANSWER:
            imgs_locator = page.locator("div.AnswerCard figure img")
        else:
            imgs_locator = page.locator("div.Post-RichTextContainer figure img")
        for i in range(await imgs_locator.count()):
            img_locator = imgs_locator.nth(i)
            await img_locator.scroll_into_view_if_needed()
        await page.wait_for_timeout(timeout=500)

        now = datetime.now()
        acted_at = dt_fromisoformat(meta["acted_at"])
        title = get_validate_filename(
            f"{item['meta']['action']}-{item['target']['title']}-{item['id'][:8]}"
        )
        target_dir = self.get_date_dir(acted_at.date()).joinpath(title)
        screenshot_path = target_dir.joinpath(f"{title}.png")
        self.logger.info(f"Saving screenshot to {screenshot_path}.")
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
            self.logger.info(f"Will fetch {len(item_list)} items")
            for item in item_list:
                await self.store_one(item, context)
                await asyncio.sleep(1)
            self.logger.info("Fetch done")
            await empty_page.close()

    async def _run(self, playwright, headless=True, **context_extra):
        if task := await self.pop_task():
            self.logger.info(f"new archive task: {task}")
            async with aiofiles.open(task.activity_path, encoding="utf-8") as fp:
                item_list = json.loads(await fp.read())
            await self.store(playwright, item_list, headless, **context_extra)
