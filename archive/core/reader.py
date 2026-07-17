import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from playwright.async_api import (
    Browser,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright_stealth import Stealth

from archive.config import settings
from archive.core.archiver import (
    TextArchive,
    ZhihuContentWorker,
    parse_archive_url,
)
from archive.core.base import Target
from archive.env import user_agent

DEFAULT_READER_QUEUE_SIZE = 8


class ReaderError(RuntimeError):
    """表示即时内容读取没有成功完成。"""


class ReaderBusyError(ReaderError):
    """表示 Reader 请求队列已满。"""


class ReaderUnavailableError(ReaderError):
    """表示 ReaderWorker 尚未就绪或已经停止。"""


class ReaderAuthStateError(ReaderError):
    """表示当前托管知乎登录态不可用于读取。"""


class ReaderRequestCancelledError(ReaderError):
    """表示调用方已经取消当前即时读取请求。"""


@dataclass
class ReaderJob:
    """表示等待 ReaderWorker 执行的一次内容读取请求。"""

    url: str
    future: asyncio.Future[TextArchive]
    deadline: float
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


class ReaderWorker(ZhihuContentWorker):
    """使用独立 Browser 即时读取知乎正文，不进入归档任务队列。"""

    name = "reader"
    output_name = ""

    def __init__(
        self,
        *args: object,
        queue_size: int = DEFAULT_READER_QUEUE_SIZE,
        **kwargs: object,
    ) -> None:
        """创建 ReaderWorker 及其有界请求队列。

        Args:
            queue_size: 允许等待执行的最大请求数量。
        """
        super().__init__(*args, **kwargs)
        self.queue: asyncio.Queue[ReaderJob] = asyncio.Queue(maxsize=queue_size)
        self._browser: Browser | None = None
        self._playwright: Playwright | None = None
        self._ready = asyncio.Event()

    @property
    def is_ready(self) -> bool:
        """返回 Reader 的 Playwright 驱动是否已经就绪。"""
        return self._ready.is_set()

    async def wait_ready(self) -> None:
        """等待 ReaderWorker 的 Playwright 驱动完成初始化。"""
        await self._ready.wait()

    async def submit(self, url: str, timeout: int) -> TextArchive:
        """提交一次即时读取并等待结果。

        Args:
            url: 知乎回答或专栏文章链接。
            timeout: 等待 Reader 返回结果的最大秒数。
        """
        if not self._ready.is_set():
            raise ReaderUnavailableError("ReaderWorker 尚未就绪")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TextArchive] = loop.create_future()
        job = ReaderJob(
            url=url,
            future=future,
            deadline=loop.time() + timeout,
        )
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull as error:
            future.cancel()
            raise ReaderBusyError("Reader 请求队列已满") from error
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as error:
            job.cancel_event.set()
            raise ReaderError("读取知乎内容超时") from error
        except asyncio.CancelledError:
            job.cancel_event.set()
            future.cancel()
            raise

    async def run_reader(self, headless: bool = True) -> None:
        """持续消费即时读取请求，并维护 Reader 独立 Browser。

        Args:
            headless: 是否使用无头浏览器。
        """
        self.logger.info("reader queue loop started.")
        try:
            async with Stealth().use_async(async_playwright()) as playwright:
                self._playwright = playwright
                self._ready.set()
                while True:
                    job = await self.queue.get()
                    try:
                        if job.future.cancelled() or job.cancel_event.is_set():
                            continue
                        archive = await self._execute_job(job, headless=headless)
                    except ReaderRequestCancelledError:
                        pass
                    except asyncio.CancelledError:
                        if not job.future.done():
                            job.future.set_exception(
                                ReaderUnavailableError("ReaderWorker 已停止")
                            )
                        raise
                    except Exception as error:
                        if isinstance(error, PlaywrightError):
                            await self._close_browser()
                        if not job.future.done():
                            job.future.set_exception(error)
                    else:
                        if not job.future.done():
                            job.future.set_result(archive)
                    finally:
                        self.queue.task_done()
        finally:
            self._ready.clear()
            await self._close_browser()
            self._playwright = None
            self._fail_pending_jobs()

    async def _execute_job(self, job: ReaderJob, headless: bool) -> TextArchive:
        """在请求剩余时限内执行实际浏览器读取。

        Args:
            job: 包含绝对截止时间的 Reader 请求。
            headless: 是否使用无头浏览器。
        """
        remaining = job.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ReaderError("读取知乎内容超时")
        read_task = asyncio.create_task(
            self._read_content(job.url, headless=headless),
            name="zhi-reader-request",
        )
        cancel_task = asyncio.create_task(
            job.cancel_event.wait(),
            name="zhi-reader-request-cancel",
        )
        try:
            done, _pending = await asyncio.wait(
                {read_task, cancel_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done or job.cancel_event.is_set():
                raise ReaderRequestCancelledError("读取请求已取消")
            if read_task in done:
                return read_task.result()
            raise ReaderError("读取知乎内容超时")
        finally:
            for task in (read_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                read_task,
                cancel_task,
                return_exceptions=True,
            )

    async def _ensure_browser(self, headless: bool) -> Browser:
        """返回可用的 Reader Browser，必要时重新启动。

        Args:
            headless: 是否使用无头浏览器。
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._playwright is None:
            raise ReaderUnavailableError("Reader Playwright 尚未初始化")
        self._browser = await getattr(
            self._playwright,
            settings.browser.value,
        ).launch(headless=headless)
        return self._browser

    async def _close_browser(self) -> None:
        """关闭 Reader 独立 Browser。"""
        browser = self._browser
        self._browser = None
        if browser is None:
            return
        try:
            await browser.close()
        except PlaywrightError:
            self.logger.warning("Reader Browser 已异常断开。")

    def _fail_pending_jobs(self) -> None:
        """在 Reader 停止时让所有排队请求立即失败。"""
        while True:
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not job.future.done():
                job.future.set_exception(ReaderUnavailableError("ReaderWorker 已停止"))
            self.queue.task_done()

    async def _read_content(self, url: str, headless: bool) -> TextArchive:
        """使用独立 BrowserContext 打开并抽取一条知乎内容。

        Args:
            url: 知乎回答或专栏文章链接。
            headless: 是否使用无头浏览器。
        """
        status = await self.auth_state.status()
        if not status["configured"] or not status["valid"]:
            raise ReaderAuthStateError(status["error"] or "知乎登录状态不可用")

        normalized_url, target_type = parse_archive_url(url)
        await self.global_configurator.load_to_worker(sync=False)
        browser = await self._ensure_browser(headless)
        context = await browser.new_context(
            storage_state=await self.get_state_path(),
            locale="zh-CN",
            user_agent=user_agent,
        )
        try:
            await self.init_context(context)
            page = await self.new_page(context)
            target = Target(
                title="",
                link=normalized_url,
                author="",
                fetched_at=datetime.now(),
            )
            await page.route(normalized_url, self.referrer_route)
            await self.goto(page, normalized_url)
            await self.fill_target_metadata(page, target, target_type)
            if not target["title"]:
                target["title"] = target_type.value
            return await self.extract_text_archive(
                page,
                target,
                target_type,
                normalized_url,
            )
        finally:
            await context.close()
