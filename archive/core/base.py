import asyncio
import contextlib
import logging
import os
import pathlib
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from typing import Any, AsyncGenerator, Callable, Coroutine, TypeAlias, TypedDict
from urllib import parse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    Route,
    async_playwright,
)
from playwright_stealth import Stealth

from archive.auth_state import AuthStateManager, AuthStateSource
from archive.config import default, settings
from archive.env import user_agent
from archive.storage import SQLiteStore, get_default_store
from archive.utils.common import dt_str


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
    action: Action | str
    target_type: TargetType | str
    acted_at: datetime | str
    raw: list["str"] | None


class ActivityItem(TypedDict):
    id: str
    meta: ActivityMeta
    target: Target
    people: str | None  # 小明赞同了小红的回答，此处指小明


def get_correct_target_type(
    action_text: str,
    target_type_text: str,
) -> TargetType | None:
    """
    将知乎动态的动作文本映射为项目关心的目标类型。

    Args:
        action_text: 动态动作文本，例如“赞同”。
        target_type_text: 动态目标类型文本，例如“回答”。
    """
    try:
        action = Action(action_text)
        if action in (Action.AGREE, Action.COLLECT):
            return TargetType(target_type_text)
        elif action == Action.ANSWER:
            return TargetType.ANSWER
        elif action == Action.POST_ARTICLE and target_type_text == TargetType.ARTICLE:
            return TargetType.ARTICLE
        elif action == Action.POST_PIN and target_type_text == TargetType.PIN:
            return TargetType.PIN
        return
    except ValueError:
        return


def abort_with(error_code: str = None):
    async def abort(route: Route):
        await route.abort(error_code)

    return abort


async def init_context(context: BrowserContext):
    context.set_default_timeout(settings.context_default_timeout)
    return context


@contextlib.asynccontextmanager
async def get_context(
    playwright: Playwright,
    state_path: str | pathlib.Path,
    state_auto_save: bool = True,
    browser_headless=True,
    init: (
        Callable[[BrowserContext], Coroutine[Any, Any, BrowserContext]] | None
    ) = init_context,
    locale="zh-CN",
    **extra,
) -> AsyncGenerator[BrowserContext, None]:
    browser: Browser = await getattr(playwright, settings.browser.value).launch(
        headless=browser_headless
    )
    context = await browser.new_context(
        storage_state=state_path,
        locale=locale,
        **extra,
        user_agent=user_agent,
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
    def __init__(
        self,
        task_id: str,
        payload: ActivityItem,
    ):
        self.id = task_id
        self.payload = payload

    @property
    def task_name(self):
        return self.id

    def as_value(self) -> str:
        return self.id

    @classmethod
    def from_value(cls, v: str) -> "ArchiveTask":
        return cls(v)

    def __str__(self):
        return f"{self.__class__.__name__}<{self.as_value()}>"

    __repr__ = __str__


SerializerT: TypeAlias = Callable[[Any], Any]


class Config:
    def __init__(
        self,
        name: str,
        serializer: SerializerT = None,
        deserializer: SerializerT = None,
        read_only=False,
        depend_on: str = None,
        getter: Callable[["BaseWorker", "Config"], Any] = None,
    ):
        self.name = name
        self.serializer = serializer
        self.deserializer = deserializer
        self.read_only = read_only
        self.depend_on = depend_on
        self._getter = getter or self.default_getter
        self._updates = []

    @staticmethod
    def default_getter(worker: "BaseWorker", cfg: "Config"):
        return getattr(worker, cfg.name, None)

    def getattr(self, worker: "BaseWorker"):
        return self._getter(worker, self)

    def to_python(self, value):
        if self.deserializer:
            return self.deserializer(value)
        return value

    def to_jsonable(self, value):
        if self.serializer:
            return self.serializer(value)
        return value

    def __repr__(self):
        return f"{self.__class__.__name__}<{self.name}>"


Cfg = Config


class ConfigFilter(str, Enum):
    ALL = "all"
    READ_ONLY = "read_only"
    WRITABLE = "writable"


class WorkStatus(str, Enum):
    RUNNING = "running"  # 正在运行
    WAITING = "waiting"  # 正在等待下次运行
    ERROR = "error"  # 后台任务异常退出


class GlobalSQLiteConfigurator:
    """
    SQLite 全局运行配置。
    """

    def __init__(self, worker: "BaseWorker") -> None:
        self.worker = worker
        self.store = worker.sqlite_store
        self.logger = self.worker.logger

    @property
    def config_names(self) -> set[str]:
        """
        获取全局配置字段名集合。
        """
        return {cfg.name for cfg in self.worker.global_configurable}

    async def get_configs(self) -> dict[str, Any]:
        """
        获取全局配置。
        """
        configs = self.worker.get_global_configs()
        stored = await self.store.get_settings("global")
        configs.update(self._extract_global_configs(stored))
        return configs

    def _extract_global_configs(self, configs: dict[str, Any]) -> dict[str, Any]:
        """
        从配置字典中提取全局字段。
        """
        return {
            key: value
            for key, value in configs.items()
            if key in self.config_names and value is not None
        }

    async def _write_configs(self, configs: dict[str, Any]) -> Any:
        """
        写入全局配置。
        """
        return await self.store.set_settings("global", configs)

    async def write_configs(self, configs: dict[str, Any]) -> None:
        """
        写入可编辑的全局配置。
        """
        loaded = self.worker.load_global_configs(configs)
        all_ = await self.get_configs()
        all_.update(loaded)
        await self._write_configs(all_)

    async def sync_from_worker(self) -> Any:
        """
        将当前 worker 中的全局配置同步到 SQLite。
        """
        return await self._write_configs(self.worker.get_global_configs())

    async def load_to_worker(self, sync: bool = True) -> None:
        """
        将 SQLite 全局配置加载到当前 worker。
        """
        self.worker.load_global_configs(await self.get_configs())


class SQLiteConfigurator:
    """
    SQLite worker 运行配置。
    """

    def __init__(self, worker: "BaseWorker"):
        self.worker = worker
        self.store = worker.sqlite_store
        self.logger = self.worker.logger

    async def _get_configs(self, filter_: ConfigFilter = ConfigFilter.ALL):
        configs = self.worker.get_configs(filter_)
        all_ = await self.store.get_settings(f"worker:{self.worker.name}")
        for cfg in self.worker.get_configurable(filter_):
            if cfg.name in all_ and not cfg.depend_on:
                configs[cfg.name] = all_[cfg.name]
        return configs

    async def get_configs(self, filter_: ConfigFilter = ConfigFilter.ALL):
        await self.worker.global_configurator.load_to_worker(sync=False)
        return await self._get_configs(filter_)

    async def _write_configs(self, configs: dict[str, Any]):
        return await self.store.set_settings(f"worker:{self.worker.name}", configs)

    async def write_writeable_configs(self, configs: dict[str, Any]):
        await self.worker.global_configurator.load_to_worker(sync=False)
        loaded = self.worker.load_configs(configs)
        all_ = await self._get_configs(ConfigFilter.WRITABLE)
        all_.update(loaded)
        writable_names = {
            cfg.name for cfg in self.worker.get_configurable(ConfigFilter.WRITABLE)
        }
        return await self._write_configs(
            {key: value for key, value in all_.items() if key in writable_names}
        )

    async def sync_from_worker(self):
        await self._write_configs(self.worker.get_configs(ConfigFilter.ALL))

    async def load_to_worker(self, include_global: bool = True) -> None:
        """把 SQLite 配置加载到 worker。

        Args:
            include_global: 是否同时加载全局配置。
        """
        if include_global:
            await self.worker.global_configurator.load_to_worker()
        configs = await self._get_configs(ConfigFilter.WRITABLE)
        if not configs:
            self.logger.info("No configs found in SQLite.")
        else:
            self.worker.load_configs(configs)


class BaseWorker:
    name = "base"
    output_name = ""
    abnormal_texts = ["您的网络环境存在异常", "请输入验证码进行验证", "意见反馈"]
    global_configurable: list[Cfg] = [
        Cfg("people"),
    ]
    configurable: list[Cfg] = [
        Cfg("page_default_timeout"),
        Cfg("interval"),
        Cfg("person_page_url", read_only=True, depend_on="people"),
        Cfg("results_dir", str, read_only=True, depend_on="people"),
        Cfg("tasks_dir", str, read_only=True, depend_on="people"),
    ]

    def __init__(
        self,
        people: str = None,
        init_state_path: str | pathlib.Path = None,
        page_default_timeout: int = 30 * 1000,
        base_results_dir: str | pathlib.Path = None,
        interval: int = 10,
        store: SQLiteStore | None = None,
    ):
        self.people = people or settings.people
        self.init_state_path = init_state_path or settings.states_dir.joinpath(
            default.state_file
        )
        self.page_default_timeout = page_default_timeout
        self._base_results_dir = base_results_dir or settings.results_dir
        self.sqlite_store = store or get_default_store()
        self.auth_state = AuthStateManager(self.sqlite_store, self.init_state_path)
        self.interval = interval
        self.logger = logging.getLogger(self.name or "default")
        self.browser_semaphore: asyncio.Semaphore | None = None
        self.init_configurable()
        self.global_configurator = GlobalSQLiteConfigurator(self)
        self.configurator = SQLiteConfigurator(self)

    def init_configurable(self):
        name_to_cfg = {cfg.name: cfg for cfg in self.configurable}
        for cfg in self.get_configurable(filter_=ConfigFilter.READ_ONLY):
            if cfg.depend_on and cfg.depend_on in name_to_cfg:
                name_to_cfg[cfg.depend_on]._updates.append(cfg)

    @property
    def person_page_url(self):
        return default.person_page_url.format(people=self.people)

    async def get_status(self) -> WorkStatus:
        return WorkStatus(await self.sqlite_store.get_worker_status(self.name))

    async def set_status(self, status: WorkStatus):
        return await self.sqlite_store.set_worker_status(self.name, status.value)

    async def get_state_path(self) -> pathlib.Path:
        """返回当前 worker 使用的应用托管 storage state 路径。"""
        return pathlib.Path(self.init_state_path)

    @lru_cache(None)
    def get_configurable(self, filter_: ConfigFilter = ConfigFilter.ALL):
        if filter_ == ConfigFilter.ALL:
            return self.configurable
        elif filter_ == ConfigFilter.READ_ONLY:
            return [c for c in self.configurable if c.read_only]
        elif filter_ == ConfigFilter.WRITABLE:
            return [c for c in self.configurable if not c.read_only]

    def get_configs(self, filter_: ConfigFilter = ConfigFilter.ALL):
        configs = {}
        for c in self.get_configurable(filter_):
            v = c.getattr(self)
            configs[c.name] = c.to_jsonable(v)
        return configs

    def load_configs(self, configs: dict[str, Any]):
        loaded = {}
        for c in self.get_configurable(ConfigFilter.WRITABLE):
            if c.name in configs:
                setattr(self, c.name, c.to_python(configs[c.name]))
                loaded[c.name] = configs[c.name]
                if c._updates:
                    for cfg in c._updates:
                        loaded[cfg.name] = cfg.to_jsonable(cfg.getattr(self))
        return loaded

    def get_global_configs(self) -> dict[str, Any]:
        """
        获取当前 worker 持有的全局配置。
        """
        configs = {}
        for cfg in self.global_configurable:
            configs[cfg.name] = cfg.to_jsonable(cfg.getattr(self))
        return configs

    def load_global_configs(self, configs: dict[str, Any]) -> dict[str, Any]:
        """
        将全局配置加载到当前 worker。
        """
        loaded = {}
        for cfg in self.global_configurable:
            if cfg.name in configs:
                setattr(self, cfg.name, cfg.to_python(configs[cfg.name]))
                loaded[cfg.name] = cfg.to_jsonable(cfg.getattr(self))
        return loaded

    async def pop_task(self) -> ArchiveTask | None:
        task = await self.sqlite_store.claim_archive_task()
        if task:
            return ArchiveTask(task["id"], payload=task["payload"])

    @property
    def results_dir(self):
        r = self._base_results_dir.joinpath(self.people, self.output_name)
        os.makedirs(r, exist_ok=True)
        return r

    @property
    def tasks_dir(self):
        r = self._base_results_dir.joinpath(self.people, "tasks")
        os.makedirs(r, exist_ok=True)
        return r

    def get_date_dir(self, dt: date) -> pathlib.Path:
        date_dir = self.results_dir.joinpath(dt.strftime("%Y/%m/%d"))
        os.makedirs(date_dir, exist_ok=True)
        return date_dir

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
    ):
        state_path = await self.get_state_path()
        state_revision = await self.auth_state.revision()
        self.logger.info(f"Currently used state path: {state_path}")
        async with get_context(
            playwright,
            state_path,
            False,
            browser_headless,
            init=self.init_context,
            **context_extra,
        ) as context:
            try:
                yield context
            finally:
                if state_auto_save:
                    payload = await context.storage_state()
                    updated = await self.auth_state.activate_if_unchanged(
                        payload,
                        AuthStateSource.WORKER,
                        state_revision,
                    )
                    if not updated:
                        self.logger.info(
                            "Storage state changed while the browser context was open; "
                            "skip stale context state write-back."
                        )

    async def goto(self, page: Page, url, **kwargs):
        self.logger.info(f"Goto: {url}")
        response = await page.goto(url, **kwargs)
        if await self.is_abnormal(response):
            await page.screenshot(
                path=settings.results_dir.joinpath(f"异常{dt_str()}.png"),
                full_page=True,
            )
            raise AbnormalError(f"{url}: \n{await response.text()}")
        return response

    async def is_abnormal(self, response: Response) -> bool:
        r = parse.urlparse(response.url)
        if "account/unhuman" in r.path:
            self.logger.error("流量异常")
            return True
        return False

    async def handle_abnormal(self, *args, **kwargs):
        self.logger.info("出现异常，暂停运行")
        await self.pause()

    async def pause(self):
        return await self.sqlite_store.set_worker_paused(self.name, True)

    async def resume(self):
        return await self.sqlite_store.set_worker_paused(self.name, False)

    async def need_pause(self) -> bool:
        return await self.sqlite_store.get_worker_paused(self.name)

    async def _run(self, playwright, headless=True, **context_extra):
        raise NotImplementedError

    async def before_run(self):
        self.logger.debug("Before run")
        await self.configurator.load_to_worker()

    async def after_run(self):
        self.logger.debug("After run")

    @contextlib.asynccontextmanager
    async def rotate(self):
        if await self.need_pause():
            self.logger.info(f"{self.name} pausing")
            while await self.need_pause():
                await asyncio.sleep(1)
            self.logger.info(f"{self.name} resumed")
        await self.before_run()
        await self.set_status(WorkStatus.RUNNING)
        cancelled = False
        try:
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            await self.set_status(WorkStatus.WAITING)
            await self.after_run()
            if not cancelled:
                await asyncio.sleep(self.interval)

    async def run(
        self,
        headless=True,
        **context_extra,
    ):
        self.logger.info(f"{self.name} started.")
        await self.configurator.load_to_worker()
        while True:
            async with Stealth().use_async(async_playwright()) as playwright:
                async with self.rotate():
                    try:
                        self.logger.debug(f"{self.name}: New loop")
                        if self.browser_semaphore is None:
                            await self._run(playwright, headless, **context_extra)
                        else:
                            async with self.browser_semaphore:
                                await self._run(playwright, headless, **context_extra)
                    except asyncio.CancelledError:
                        raise
                    except AbnormalError as e:
                        self.logger.error(e)
                        await self.handle_abnormal()
                    except Exception as e:
                        self.logger.exception(e)

    async def test_state(self, state_path: pathlib.Path | str):
        """
        测试当前state/cookie是否有效（是否处于登录状态）
        """
        result = {
            "test_url": "https://www.zhihu.com/",  # 未登录访问此地址会跳转到登录页面: https://www.zhihu.com/signin?next=%2F
            "ok": True,
        }
        async with Stealth().use_async(async_playwright()) as playwright:
            async with get_context(
                playwright,
                state_path=state_path,
                state_auto_save=False,
                browser_headless=True,
                init=self.init_context,
            ) as context:
                page = await self.new_page(context)
                response = await self.goto(page, result["test_url"])
                r = parse.urlparse(response.url)
                result["test_url"] = response.url
                if r.path == "signin":
                    result["ok"] = False
                return result
