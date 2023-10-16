import asyncio
import logging
import pathlib
from enum import Enum
from urllib import parse

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from redis import asyncio as aioredis

from .config import settings
from .core import init_context
from .env import user_agent

logger = logging.getLogger("archive")


def at_home(url):
    r = parse.urlparse(url)
    if r.path == "" or r.path == "/":
        return True
    return False


class QRCodeTaskStatus(str, Enum):
    PENDING = "pending"
    FAILED = "failed"
    OK = "ok"
    NO_EXIST = "not_exist"


class QRCodeTask:
    def __init__(self, qrcode_path, state_path):
        self.qrcode_path = pathlib.Path(qrcode_path).resolve()
        self.state_path = pathlib.Path(state_path).resolve()

    @property
    def task_name(self) -> str:
        return str(self.qrcode_path)

    def as_value(self) -> str:
        return f"{self.qrcode_path}:{self.state_path}"

    @classmethod
    def from_value(cls, v: str) -> "QRCodeTask":
        qrcode_path, state_path = v.rsplit(":", maxsplit=1)
        return cls(qrcode_path, state_path)

    def __str__(self):
        return f"{self.__class__.__name__}<{self.as_value()}>"

    __repr__ = __str__


class Base:
    redis_key_prefix = "zhi_archive:login"
    qrcode_task_key = f"{redis_key_prefix}:qrcode_task"
    qrcode_task_result_key = f"{redis_key_prefix}:qrcode_task_result"

    def __init__(self, redis_url: str = settings.redis_url):
        self.redis = aioredis.from_url(
            redis_url,
            password=settings.redis_passwd,
            encoding="utf-8",
            decode_responses=True,
        )

    async def new_task(self, task: QRCodeTask):
        await self.redis.set(self.qrcode_task_key, task.as_value())

    async def get_qrcode_task(self) -> QRCodeTask | None:
        task = await self.redis.get(self.qrcode_task_key)
        if not task:
            return
        return QRCodeTask.from_value(task)

    async def get_qrcode_task_status(self, task_name: str) -> QRCodeTaskStatus:
        status = await self.redis.hget(self.qrcode_task_result_key, task_name)
        try:
            return QRCodeTaskStatus(status)
        except ValueError:
            return QRCodeTaskStatus.NO_EXIST

    async def set_qrcode_task_status(self, task_name: str, status: QRCodeTaskStatus):
        result = await self.redis.hset(
            self.qrcode_task_result_key, task_name, status.value
        )
        return result


class ZhiLoginClient(Base):
    pass


class ZhiLogin(Base):
    redis_key_prefix = "zhi_archive:login"
    qrcode_task_key = f"{redis_key_prefix}:qrcode_task"
    qrcode_task_result_key = f"{redis_key_prefix}:qrcode_task_result"

    def __init__(
        self,
        scan_timeout: int = 1000 * 60 * 3,
        redis_url: str = settings.redis_url,
        headless=True,
        **context_extra,
    ):
        super().__init__(redis_url)
        self.scan_timeout = scan_timeout
        self.headless = headless
        context_extra.setdefault("user_agent", user_agent)
        self.context_extra = context_extra

    async def get_new_req(self) -> QRCodeTask | None:
        task = await self.get_qrcode_task()
        if not task:
            return
        qrcode_path = task.qrcode_path
        if (
            qrcode_path
            and (await self.get_qrcode_task_status(task.task_name))
            == QRCodeTaskStatus.NO_EXIST
        ):
            return task
        return

    async def run(self):
        async with async_playwright() as playwright:
            while True:
                try:
                    if qrcode_task := await self.get_new_req():
                        logger.info(f"new qrcode task: {qrcode_task}")
                        await self.get_qrcode(playwright, qrcode_task)
                except Exception as e:
                    logger.exception(e)
                await asyncio.sleep(1)

    async def _wait_for_login_success(self, page: Page, task_key: str):
        try:
            await page.wait_for_url(at_home, timeout=self.scan_timeout)
            await self.set_qrcode_task_status(task_key, QRCodeTaskStatus.OK)
        except PlaywrightTimeoutError:
            await self.set_qrcode_task_status(task_key, QRCodeTaskStatus.FAILED)
        finally:
            await page.close()

    async def _wait_qrcode(self, page: Page, qrcode_path: pathlib.Path | str = None):
        img_bytes = await page.locator("img.Qrcode-qrcode").screenshot(
            type="png", path=qrcode_path
        )
        if len(img_bytes) < 4096 + 100:
            return await self._wait_qrcode(page, qrcode_path)

    async def get_qrcode(
        self,
        playwright: Playwright,
        qrcode_task: QRCodeTask,
    ) -> bytes:
        browser: Browser = await getattr(playwright, settings.browser.value).launch(
            headless=self.headless
        )
        context = await browser.new_context(**self.context_extra)
        await init_context(context)
        async with context:
            await self.set_qrcode_task_status(
                qrcode_task.task_name, QRCodeTaskStatus.PENDING
            )
            page = await context.new_page()
            await page.goto("https://www.zhihu.com/signin?next=%2F")
            _ = await self._wait_qrcode(page)
            img_bytes = await self._wait_qrcode(page, qrcode_task.qrcode_path)

            await self._wait_for_login_success(page, qrcode_task.task_name)
            await context.storage_state(path=qrcode_task.state_path)
            return img_bytes
