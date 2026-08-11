import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from archive.auth_state import AuthStateManager
from archive.config import settings
from archive.core.archiver import ArchiveQueueService, Archiver
from archive.core.base import WorkStatus
from archive.core.login import QRCodeTask, QRCodeTaskStatus, ZhiLogin
from archive.core.monitor import Monitor
from archive.core.reader import ReaderUnavailableError, ReaderWorker
from archive.mcp_config import MCPConfigManager
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
        self.mcp_config = MCPConfigManager(self.store)
        self.archive_event = asyncio.Event()
        self.archive_queue = ArchiveQueueService(self.store, self.archive_event)
        self.worker_browser_semaphore = asyncio.Semaphore(1)
        self.interactive_browser_semaphore = asyncio.Semaphore(1)
        self.monitor = Monitor(store=self.store)
        self.archiver = Archiver(store=self.store)
        self.reader = ReaderWorker(store=self.store)
        self.monitor.archive_event = self.archive_event
        self.monitor.browser_semaphore = self.worker_browser_semaphore
        self.archiver.browser_semaphore = self.worker_browser_semaphore
        self.monitor_task: asyncio.Task[Any] | None = None
        self.archiver_task: asyncio.Task[Any] | None = None
        self.archiver_start_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[Any] | None = None
        self.reader_start_task: asyncio.Task[None] | None = None
        self.reader_start_lock = asyncio.Lock()
        self.login_task: asyncio.Task[Any] | None = None
        self.worker_errors: dict[str, str] = {}

    async def start(self) -> None:
        """初始化 SQLite 并启动受监督的后台任务。"""
        await self.store.connect()
        try:
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
            self.archiver_task = self._start_archiver_worker()
            if await self.store.has_pending_archive_tasks():
                self.archive_event.set()
        except BaseException:
            await self.stop()
            raise

    async def ensure_reader_started(self) -> None:
        """按需启动 Reader，并等待 Playwright 驱动就绪。"""
        if self.reader_task is not None and not self.reader_task.done():
            if self.reader.is_ready:
                return
        async with self.reader_start_lock:
            if self.reader_task is not None and not self.reader_task.done():
                if self.reader.is_ready:
                    return
            start_task = self.reader_start_task
            if start_task is None or start_task.done():
                start_task = asyncio.create_task(
                    self._start_reader(),
                    name="zhi-reader-start",
                )
                start_task.add_done_callback(self._on_reader_start_done)
                self.reader_start_task = start_task
        await asyncio.shield(start_task)

    def _start_archiver_worker(self) -> asyncio.Task[Any]:
        """创建受监督的 Archiver 队列任务。"""
        return self._create_supervised_task(
            "archiver",
            self.archiver.run_queue(
                self.archive_event,
                headless=settings.archiver_headless,
            ),
        )

    async def get_archiver_status(self) -> dict[str, Any]:
        """返回 MCP 可安全展示的 Archiver 运行状态。"""
        control = await self.store.get_worker_control("archiver")
        task = self.archiver_task
        return {
            "paused": bool(control["paused"]),
            "status": str(control["status"]),
            "worker_alive": task is not None and not task.done(),
            "last_error": control["last_error"] or self.worker_errors.get("archiver"),
        }

    async def resume_archiver(self) -> dict[str, Any]:
        """解除 Archiver 暂停状态，必要时重启异常退出的队列任务。"""
        async with self.archiver_start_lock:
            task = self.archiver_task
            if task is None or task.done():
                await self.store.recover_running_archive_tasks()
                await self.store.set_worker_status(
                    "archiver",
                    WorkStatus.WAITING.value,
                )
                self.worker_errors.pop("archiver", None)
                self.archiver_task = self._start_archiver_worker()
            await self.archiver.resume()
            self.archive_event.set()
        return await self.get_archiver_status()

    def _on_reader_start_done(self, task: asyncio.Task[None]) -> None:
        """回收无人等待的 Reader 启动异常，避免调用方取消后泄漏任务错误。

        Args:
            task: 已结束的 Reader 启动任务。
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("Reader startup did not complete: %s", error)

    async def _start_reader(self) -> None:
        """启动唯一的 Reader 后台任务并等待其就绪。"""
        self.worker_errors.pop("reader", None)
        reader_task = self.reader_task
        if reader_task is None or reader_task.done():
            reader_task = self._create_supervised_task(
                "reader",
                self.reader.run_reader(headless=settings.reader_headless),
            )
            self.reader_task = reader_task
        ready_task = asyncio.create_task(
            self.reader.wait_ready(),
            name="zhi-reader-ready",
        )
        try:
            done, _pending = await asyncio.wait(
                {ready_task, reader_task},
                timeout=10,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if reader_task in done:
                error = None if reader_task.cancelled() else reader_task.exception()
                message = "ReaderWorker 初始化失败"
                if error is not None:
                    raise ReaderUnavailableError(message) from error
                raise ReaderUnavailableError(message)
            if ready_task in done and self.reader.is_ready:
                return
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            raise ReaderUnavailableError("ReaderWorker 初始化超时")
        except asyncio.CancelledError:
            if not reader_task.done():
                reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            raise
        finally:
            if not ready_task.done():
                ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)

    async def stop(self) -> None:
        """取消后台任务并关闭 SQLite 连接。"""
        self.archive_event.set()
        tasks = [
            self.monitor_task,
            self.archiver_task,
            self.reader_start_task,
            self.reader_task,
            self.login_task,
        ]
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
        if any(name != "reader" for name in self.worker_errors):
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
