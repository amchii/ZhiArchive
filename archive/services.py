import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from archive.auth_state import AuthStateManager
from archive.config import settings
from archive.core.archiver import Archiver
from archive.core.base import WorkStatus
from archive.core.login import QRCodeTask, QRCodeTaskStatus, ZhiLogin
from archive.core.monitor import Monitor
from archive.storage import SQLiteStore

logger = logging.getLogger("api")


class AppServices:
    """持有单进程应用运行所需的共享服务和后台任务。"""

    def __init__(self, store: SQLiteStore | None = None) -> None:
        """创建应用级服务容器。

        Args:
            store: 可注入的 SQLite store，默认使用配置中的数据库路径。
        """
        self.store = store or SQLiteStore()
        self.auth_state = AuthStateManager(self.store)
        self.archive_event = asyncio.Event()
        self.worker_browser_semaphore = asyncio.Semaphore(1)
        self.interactive_browser_semaphore = asyncio.Semaphore(1)
        self.monitor = Monitor(store=self.store)
        self.archiver = Archiver(store=self.store)
        self.monitor.archive_event = self.archive_event
        self.archiver.archive_event = self.archive_event
        self.monitor.browser_semaphore = self.worker_browser_semaphore
        self.archiver.browser_semaphore = self.worker_browser_semaphore
        self.monitor_task: asyncio.Task[Any] | None = None
        self.archiver_task: asyncio.Task[Any] | None = None
        self.login_task: asyncio.Task[Any] | None = None
        self.worker_errors: dict[str, str] = {}

    async def start(self) -> None:
        """初始化 SQLite 并启动受监督的后台任务。"""
        await self.store.connect()
        await self.store.seed_defaults()
        await self.auth_state.migrate_legacy_path()
        await self.store.recover_running_archive_tasks()
        await self.store.fail_incomplete_login_tasks()
        logger.warning(
            "ZhiArchive is running in single-process SQLite mode; "
            "run only one Uvicorn worker and one application instance."
        )
        self.monitor_task = self._create_supervised_task(
            "monitor",
            self.monitor.run(headless=settings.monitor_headless),
        )
        self.archiver_task = self._create_supervised_task(
            "archiver",
            self.archiver.run_queue(
                self.archive_event,
                headless=settings.archiver_headless,
            ),
        )
        if await self.store.has_pending_archive_tasks():
            self.archive_event.set()

    async def stop(self) -> None:
        """取消后台任务并关闭 SQLite 连接。"""
        self.archive_event.set()
        tasks = [self.monitor_task, self.archiver_task, self.login_task]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
        await self.store.fail_incomplete_login_tasks()
        await self.store.close()

    def _create_supervised_task(
        self,
        name: str,
        coro: Any,
    ) -> asyncio.Task[Any]:
        """创建后台任务并在异常退出时记录健康状态。"""
        task = asyncio.create_task(coro, name=f"zhi-{name}")
        task.add_done_callback(lambda done: self._on_worker_done(name, done))
        return task

    def _on_worker_done(self, name: str, task: asyncio.Task[Any]) -> None:
        """记录非预期结束的后台任务。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        message = repr(error)
        self.worker_errors[name] = message
        logger.critical("%s worker exited unexpectedly: %s", name, message)
        asyncio.create_task(
            self.store.set_worker_status(name, WorkStatus.ERROR.value, message)
        )

    def healthy(self) -> bool:
        """返回应用关键后台任务是否健康。"""
        if self.worker_errors:
            return False
        for task in (self.monitor_task, self.archiver_task):
            if task is not None and task.done() and not task.cancelled():
                return False
        return True

    async def start_login_task(self, task: QRCodeTask) -> QRCodeTask:
        """创建并启动一次二维码登录后台任务。"""
        expires_at = datetime.now() + timedelta(seconds=ZhiLogin.task_timeout)
        row = await self.store.create_login_task(
            task.id,
            task.qrcode_path,
            expires_at,
        )
        active_task = QRCodeTask(row["qrcode_path"])
        if active_task.id != task.id:
            return active_task
        if self.login_task is not None and not self.login_task.done():
            return active_task
        self.login_task = asyncio.create_task(
            self._run_login_task(active_task),
            name="zhi-login",
        )
        return active_task

    async def _run_login_task(self, task: QRCodeTask) -> None:
        """执行二维码生成、扫码等待和 state 保存流程。"""
        login = ZhiLogin(
            headless=settings.login_worker_headless,
            store=self.store,
            auth_state=self.auth_state,
        )
        try:
            async with self.interactive_browser_semaphore:
                async with Stealth().use_async(async_playwright()) as playwright:
                    await login.get_qrcode(playwright, task)
        except asyncio.CancelledError:
            await self.store.set_login_task_status(
                task.id,
                QRCodeTaskStatus.FAILED.value,
                "应用关闭，登录任务已取消",
            )
            raise
        except Exception as error:
            logger.exception(error)
            await self.store.set_login_task_status(
                task.id,
                QRCodeTaskStatus.FAILED.value,
                str(error),
            )


_current_services: AppServices | None = None


def set_current_services(services: AppServices | None) -> None:
    """设置当前 FastAPI 进程的服务容器。"""
    global _current_services
    _current_services = services


def get_current_services() -> AppServices | None:
    """读取当前 FastAPI 进程的服务容器。"""
    return _current_services


def new_qrcode_task() -> QRCodeTask:
    """创建新的二维码登录任务路径。"""
    prefix = os.urandom(10).hex()
    return QRCodeTask(settings.states_dir.joinpath(f"{prefix}.qrcode.png"))
