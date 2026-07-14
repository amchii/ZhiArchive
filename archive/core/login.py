import logging
import pathlib
from datetime import datetime, timedelta
from enum import Enum
from urllib import parse

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from archive.auth_state import AuthStateManager, AuthStateSource
from archive.config import settings
from archive.env import user_agent
from archive.storage import SQLiteStore, get_default_store

from .base import init_context

logger = logging.getLogger("login_worker")


def normalize_task_name(task_name: str) -> str:
    """把旧版二维码路径或新版前缀统一转换为登录任务 ID。"""
    name = pathlib.Path(str(task_name)).name
    return name.split(".", maxsplit=1)[0]


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
    WAITING_FOR_SCAN = "waiting_for_scan"


class QRCodeTask:
    def __init__(self, qrcode_path: pathlib.Path | str) -> None:
        """创建只持有二维码文件位置的登录任务。"""
        self.qrcode_path = pathlib.Path(qrcode_path).resolve()

    @property
    def id(self) -> str:
        """返回登录任务前缀 ID。"""
        return self.qrcode_path.name.split(".", maxsplit=1)[0]

    @property
    def task_name(self) -> str:
        """返回兼容调用方命名的登录任务 ID。"""
        return self.id

    def __str__(self) -> str:
        """返回适合日志记录的二维码任务文本。"""
        return f"{self.__class__.__name__}<{self.qrcode_path}>"

    __repr__ = __str__


class Base:
    task_timeout = 60 * 5

    def __init__(
        self,
        store: SQLiteStore | None = None,
    ):
        self.store = store or get_default_store()

    async def new_task(self, task: QRCodeTask) -> QRCodeTask:
        """
        创建二维码登录任务。

        Args:
            task: 待创建的二维码登录任务。
        """
        row = await self.store.create_login_task(
            task.id,
            task.qrcode_path,
            datetime.now() + timedelta(seconds=self.task_timeout),
        )
        return QRCodeTask(row["qrcode_path"])

    async def get_qrcode_task_status(self, task_name: str) -> QRCodeTaskStatus:
        """
        读取二维码登录任务状态。

        Args:
            task_name: 登录任务 ID 或旧版任务路径。
        """
        status = await self.store.get_login_task_status(normalize_task_name(task_name))
        try:
            return QRCodeTaskStatus(status)
        except ValueError:
            return QRCodeTaskStatus.NO_EXIST

    async def set_qrcode_task_status(self, task_name: str, status: QRCodeTaskStatus):
        """
        更新二维码登录任务状态。

        Args:
            task_name: 登录任务 ID 或旧版任务路径。
            status: 新状态。
        """
        await self.store.set_login_task_status(
            normalize_task_name(task_name),
            status.value,
        )


class ZhiLoginClient(Base):
    pass


class ZhiLogin(Base):
    def __init__(
        self,
        scan_timeout: int = 1000 * 60 * 3,
        store: SQLiteStore | None = None,
        auth_state: AuthStateManager | None = None,
        headless=True,
        **context_extra,
    ):
        super().__init__(store=store)
        self.auth_state = auth_state or AuthStateManager(self.store)
        self.scan_timeout = scan_timeout
        self.headless = headless
        context_extra.setdefault("user_agent", user_agent)
        self.context_extra = context_extra

    async def _wait_for_login_success(self, page: Page, task_key: str) -> bool:
        """等待扫码完成，并返回是否成功进入知乎首页。"""
        logger.info(f"等待扫码登录: {task_key}")
        try:
            await self.set_qrcode_task_status(
                task_key, QRCodeTaskStatus.WAITING_FOR_SCAN
            )
            await page.wait_for_url(at_home, timeout=self.scan_timeout)
            logger.info(f"登录成功: {task_key}")
            return True
        except PlaywrightTimeoutError:
            logger.info(f"登录超时: {task_key}")
            await self.set_qrcode_task_status(task_key, QRCodeTaskStatus.FAILED)
            return False
        finally:
            await page.close()

    async def _wait_qrcode(self, page: Page, qrcode_path: pathlib.Path | str = None):
        img_bytes = await page.locator("div.Qrcode-img").screenshot(
            type="png", path=qrcode_path
        )
        # 确保二维码图片有效, 默认占位图片大概2.7KB，二维码图片大概7KB
        if len(img_bytes) < 4096 + 100:
            logger.info(f"二维码保存成功: {qrcode_path}")
            return await self._wait_qrcode(page, qrcode_path)

    async def get_qrcode(
        self,
        playwright: Playwright,
        qrcode_task: QRCodeTask,
    ) -> bytes:
        browser: Browser = await getattr(playwright, settings.browser.value).launch(
            headless=self.headless
        )
        try:
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

                logged_in = await self._wait_for_login_success(
                    page,
                    qrcode_task.task_name,
                )
                if not logged_in:
                    return img_bytes
                state = await context.storage_state()
                await self.auth_state.activate(state, AuthStateSource.QRCODE)
                await self.set_qrcode_task_status(
                    qrcode_task.task_name,
                    QRCodeTaskStatus.OK,
                )
                logger.info("二维码登录状态已写入托管 state")
                return img_bytes
        finally:
            await browser.close()
