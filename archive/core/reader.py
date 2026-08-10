import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from playwright.async_api import (
    Browser,
    BrowserContext,
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
from archive.core.profile import (
    ProfileContentType,
    ProfilePage,
    ProfileRateLimitError,
    read_profile_page,
)
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


class ReaderProfileCooldownError(ReaderError):
    """表示个人列表请求因知乎风控响应而处于本地冷却期。"""


@dataclass
class ReaderJob:
    """表示等待 ReaderWorker 执行的一次内容读取请求。"""

    url: str
    future: asyncio.Future[TextArchive]
    deadline: float
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ProfileReaderJob:
    """表示等待 ReaderWorker 执行的一次个人列表读取请求。"""

    content_type: ProfileContentType
    people: str | None
    offset: int
    limit: int
    collection_id: str | None
    future: asyncio.Future[ProfilePage]
    deadline: float
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


ReaderQueueJob = ReaderJob | ProfileReaderJob
ReaderResult = TextArchive | ProfilePage
ProfileCacheKey = tuple[
    ProfileContentType,
    str | None,
    int,
    int,
    str | None,
    str | None,
]


@dataclass
class ProfileCacheEntry:
    """表示一页个人内容的短期内存缓存。"""

    expires_at: float
    page: ProfilePage


def normalize_profile_seconds(value: float, name: str) -> float:
    """校验个人列表风控配置并返回非负秒数。

    Args:
        value: 待校验的秒数配置。
        name: 用于错误提示的配置名称。
    """
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{name} 必须是非负有限数值")
    return seconds


class ReaderWorker(ZhihuContentWorker):
    """使用独立 Browser 即时读取知乎正文和个人列表，不进入归档队列。"""

    name = "reader"
    output_name = ""

    def __init__(
        self,
        *args: object,
        queue_size: int = DEFAULT_READER_QUEUE_SIZE,
        profile_request_min_interval_seconds: float | None = None,
        profile_request_jitter_seconds: float | None = None,
        profile_cache_ttl_seconds: float | None = None,
        profile_cooldown_base_seconds: float | None = None,
        profile_cooldown_max_seconds: float | None = None,
        **kwargs: object,
    ) -> None:
        """创建 ReaderWorker 及其有界请求队列。

        Args:
            queue_size: 允许等待执行的最大请求数量。
            profile_request_min_interval_seconds: 两次个人列表请求的最小间隔。
            profile_request_jitter_seconds: 个人列表请求间隔的随机抖动上限。
            profile_cache_ttl_seconds: 相同个人列表分页结果的缓存时间。
            profile_cooldown_base_seconds: 首次风控响应后的基础冷却时间。
            profile_cooldown_max_seconds: 指数退避的最大本地冷却时间。
        """
        super().__init__(*args, **kwargs)
        self.queue: asyncio.Queue[ReaderQueueJob] = asyncio.Queue(maxsize=queue_size)
        self._browser: Browser | None = None
        self._playwright: Playwright | None = None
        self._ready = asyncio.Event()
        self.profile_request_min_interval_seconds = normalize_profile_seconds(
            profile_request_min_interval_seconds
            if profile_request_min_interval_seconds is not None
            else settings.profile_request_min_interval_seconds,
            "profile_request_min_interval_seconds",
        )
        self.profile_request_jitter_seconds = normalize_profile_seconds(
            profile_request_jitter_seconds
            if profile_request_jitter_seconds is not None
            else settings.profile_request_jitter_seconds,
            "profile_request_jitter_seconds",
        )
        self.profile_cache_ttl_seconds = normalize_profile_seconds(
            profile_cache_ttl_seconds
            if profile_cache_ttl_seconds is not None
            else settings.profile_cache_ttl_seconds,
            "profile_cache_ttl_seconds",
        )
        self.profile_cooldown_base_seconds = normalize_profile_seconds(
            profile_cooldown_base_seconds
            if profile_cooldown_base_seconds is not None
            else settings.profile_cooldown_base_seconds,
            "profile_cooldown_base_seconds",
        )
        configured_cooldown_max = normalize_profile_seconds(
            profile_cooldown_max_seconds
            if profile_cooldown_max_seconds is not None
            else settings.profile_cooldown_max_seconds,
            "profile_cooldown_max_seconds",
        )
        self.profile_cooldown_max_seconds = max(
            self.profile_cooldown_base_seconds,
            configured_cooldown_max,
        )
        self._profile_last_request_at: float | None = None
        self._profile_cooldown_until = 0.0
        self._profile_rate_limit_failures = 0
        self._profile_cache: dict[ProfileCacheKey, ProfileCacheEntry] = {}

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

    async def submit_profile(
        self,
        *,
        content_type: ProfileContentType,
        people: str | None,
        offset: int,
        limit: int,
        collection_id: str | None,
        timeout: int,
    ) -> ProfilePage:
        """提交一次个人列表读取并等待结果。

        Args:
            content_type: 待读取的个人内容类型。
            people: 个人列表对应的知乎用户 ID。
            offset: 当前分页偏移量。
            limit: 当前分页条目数。
            collection_id: 收藏夹内容列表对应的收藏夹 ID。
            timeout: 等待 Reader 返回结果的最大秒数。
        """
        if not self._ready.is_set():
            raise ReaderUnavailableError("ReaderWorker 尚未就绪")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ProfilePage] = loop.create_future()
        job = ProfileReaderJob(
            content_type=content_type,
            people=people,
            offset=offset,
            limit=limit,
            collection_id=collection_id,
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
            raise ReaderError("读取知乎个人列表超时") from error
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
                        result = await self._execute_job(job, headless=headless)
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
                            job.future.set_result(result)  # type: ignore[arg-type]
                    finally:
                        self.queue.task_done()
        finally:
            self._ready.clear()
            await self._close_browser()
            self._playwright = None
            self._fail_pending_jobs()

    async def _execute_job(
        self,
        job: ReaderQueueJob,
        headless: bool,
    ) -> ReaderResult:
        """在请求剩余时限内执行实际浏览器读取。

        Args:
            job: 包含绝对截止时间的 Reader 请求。
            headless: 是否使用无头浏览器。
        """
        remaining = job.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ReaderError("读取知乎内容超时")
        if isinstance(job, ProfileReaderJob):
            read_coro = self._read_profile(
                content_type=job.content_type,
                people=job.people,
                offset=job.offset,
                limit=job.limit,
                collection_id=job.collection_id,
                headless=headless,
            )
            task_name = "zhi-profile-reader-request"
        else:
            read_coro = self._read_content(job.url, headless=headless)
            task_name = "zhi-reader-request"
        read_task = asyncio.create_task(read_coro, name=task_name)
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

    async def _new_reader_context(self, browser: Browser) -> BrowserContext:
        """创建并初始化一次性 Reader BrowserContext。

        Args:
            browser: ReaderWorker 复用的独立 Browser。
        """
        context = await browser.new_context(
            storage_state=await self.get_state_path(),
            locale="zh-CN",
            user_agent=user_agent,
        )
        try:
            await self.init_context(context)
        except BaseException:
            await context.close()
            raise
        return context

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

    def _get_cached_profile_page(
        self,
        key: ProfileCacheKey,
        now: float,
    ) -> ProfilePage | None:
        """返回尚未过期的个人列表缓存并清理旧条目。

        Args:
            key: 个人列表分页的稳定缓存键。
            now: 当前事件循环单调时间。
        """
        expired_keys = [
            cache_key
            for cache_key, entry in self._profile_cache.items()
            if entry.expires_at <= now
        ]
        for cache_key in expired_keys:
            self._profile_cache.pop(cache_key, None)
        entry = self._profile_cache.get(key)
        return entry.page.model_copy(deep=True) if entry is not None else None

    def _cache_profile_page(
        self,
        key: ProfileCacheKey,
        page: ProfilePage,
        now: float,
    ) -> None:
        """把个人列表分页保存到短期内存缓存。

        Args:
            key: 个人列表分页的稳定缓存键。
            page: 已成功解析的个人列表分页。
            now: 当前事件循环单调时间。
        """
        if self.profile_cache_ttl_seconds <= 0:
            return
        self._profile_cache[key] = ProfileCacheEntry(
            expires_at=now + self.profile_cache_ttl_seconds,
            page=page.model_copy(deep=True),
        )

    async def _wait_for_profile_request_slot(self) -> None:
        """执行个人列表请求前检查冷却期并应用随机请求间隔。"""
        loop = asyncio.get_running_loop()
        now = loop.time()
        cooldown_remaining = self._profile_cooldown_until - now
        if cooldown_remaining > 0:
            raise ReaderProfileCooldownError(
                "知乎个人列表请求正在冷却，"
                f"请约 {math.ceil(cooldown_remaining)} 秒后再试"
            )
        if self._profile_last_request_at is None:
            return
        jitter = random.uniform(0.0, self.profile_request_jitter_seconds)
        delay = (
            self._profile_last_request_at
            + self.profile_request_min_interval_seconds
            + jitter
            - now
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _open_profile_circuit(self, error: ProfileRateLimitError) -> None:
        """根据知乎风控响应开启指数退避冷却。

        Args:
            error: 包含响应状态和 Retry-After 的风控错误。
        """
        self._profile_rate_limit_failures += 1
        exponent = min(self._profile_rate_limit_failures - 1, 10)
        cooldown = min(
            self.profile_cooldown_base_seconds * (2**exponent),
            self.profile_cooldown_max_seconds,
        )
        if error.retry_after is not None:
            cooldown = min(
                max(cooldown, error.retry_after),
                self.profile_cooldown_max_seconds,
            )
        now = asyncio.get_running_loop().time()
        self._profile_cooldown_until = max(
            self._profile_cooldown_until,
            now + cooldown,
        )

    def _close_profile_circuit(self) -> None:
        """在请求成功后清除个人列表风控失败计数。"""
        self._profile_rate_limit_failures = 0
        self._profile_cooldown_until = 0.0

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
        context = await self._new_reader_context(browser)
        try:
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

    async def _read_profile(
        self,
        *,
        content_type: ProfileContentType,
        people: str | None,
        offset: int,
        limit: int,
        collection_id: str | None,
        headless: bool,
    ) -> ProfilePage:
        """使用独立 BrowserContext 读取一页知乎个人内容。

        Args:
            content_type: 待读取的个人内容类型。
            people: 个人列表对应的知乎用户 ID。
            offset: 当前分页偏移量。
            limit: 当前分页条目数。
            collection_id: 收藏夹内容列表对应的收藏夹 ID。
            headless: 是否使用无头浏览器。
        """
        status = await self.auth_state.status()
        if not status["configured"] or not status["valid"]:
            raise ReaderAuthStateError(status["error"] or "知乎登录状态不可用")

        cache_key: ProfileCacheKey = (
            content_type,
            people,
            offset,
            limit,
            collection_id,
            (
                status["updated_at"].isoformat()
                if status.get("updated_at") is not None
                else None
            ),
        )
        loop = asyncio.get_running_loop()
        cached_page = self._get_cached_profile_page(cache_key, loop.time())
        if cached_page is not None:
            return cached_page

        await self._wait_for_profile_request_slot()
        browser = await self._ensure_browser(headless)
        context = await self._new_reader_context(browser)
        try:
            self._profile_last_request_at = loop.time()
            try:
                page = await read_profile_page(
                    context.request,
                    content_type=content_type,
                    people=people,
                    offset=offset,
                    limit=limit,
                    collection_id=collection_id,
                    timeout=self.page_default_timeout,
                )
            except ProfileRateLimitError as error:
                self._open_profile_circuit(error)
                raise
            self._close_profile_circuit()
            self._cache_profile_page(cache_key, page, loop.time())
            return page
        finally:
            await context.close()
