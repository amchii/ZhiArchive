import asyncio
import contextlib
import json
import logging
import os
import pathlib
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from typing import Any, Callable, Coroutine, TypeAlias, TypedDict
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
from redis import asyncio as aioredis

from archive.config import default, settings
from archive.env import user_agent
from archive.utils.common import dt_str
from archive.utils.encoder import JSONEncoder
from archive.utils.stealth import stealth_async


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
    action: str
    target_type: str
    acted_at: datetime | str
    raw: list["str"] | None


class ActivityItem(TypedDict):
    id: str
    meta: ActivityMeta
    target: Target


def get_correct_target_type(action_text, target_type_text) -> TargetType | None:
    try:
        action = Action(action_text)
        if action in (Action.AGREE, Action.COLLECT):
            return TargetType(target_type_text)
        elif action == Action.ANSWER:
            return TargetType.ANSWER
        elif action == Action.POST_ARTICLE and target_type_text == TargetType.ARTICLE:
            return TargetType.ARTICLE
        return
    except ValueError:
        return


def abort_with(error_code: str = None):
    async def abort(route: Route):
        await route.abort(error_code)

    return abort


async def init_context(context: BrowserContext):
    context.set_default_timeout(settings.context_default_timeout)
    await stealth_async(context)
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
) -> BrowserContext:
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
    def __init__(self, activity_info_path):
        self.activity_path = pathlib.Path(activity_info_path).resolve()

    @property
    def task_name(self):
        return str(self.activity_path)

    def as_value(self) -> str:
        return f"{self.activity_path}"

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


class RedisConfigurator:
    """
    Redis配置
    """

    def __init__(self, worker: "BaseWorker"):
        self.worker = worker
        self.redis = worker.redis
        self.configs_key = worker.configs_key
        self.logger = self.worker.logger

    async def get_configs(self, filter_: ConfigFilter = ConfigFilter.ALL):
        configs = {}
        if configs_str := await self.redis.get(self.configs_key):
            all_ = json.loads(configs_str)
            for c in self.worker.get_configurable(filter_):
                if c.name in all_:
                    configs[c.name] = all_[c.name]
        return configs

    async def _write_configs(self, configs: dict[str, Any]):
        return await self.redis.set(
            self.configs_key, json.dumps(configs, cls=JSONEncoder)
        )

    async def write_writeable_configs(self, configs: dict[str, Any]):
        loaded = self.worker.load_configs(configs)
        all_ = await self.get_configs()
        all_.update(loaded)
        return await self._write_configs(all_)

    async def sync_from_worker(self):
        await self._write_configs(self.worker.get_configs(ConfigFilter.ALL))

    async def load_to_worker(self):
        configs = await self.get_configs()
        if not configs:
            self.logger.info("No configs found in redis.")
        else:
            self.worker.load_configs(await self.get_configs())
        await self.sync_from_worker()


class BaseWorker:
    name = ""
    output_name = ""
    redis_key_prefix = "zhi_archive:archive"
    state_path_key = f"{redis_key_prefix}:state_path"
    tasks_key = f"{redis_key_prefix}:tasks"  # list
    tasks_result_key = f"{redis_key_prefix}:task_results"  # hash
    abnormal_texts = ["您的网络环境存在异常", "请输入验证码进行验证", "意见反馈"]
    configurable: list[Cfg] = [
        Cfg("people"),
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
        redis_url: str = settings.redis_url,
        interval: int = 10,
    ):
        self.people = people or settings.people
        self.init_state_path = init_state_path or settings.states_dir.joinpath(
            default.state_file
        )
        self.page_default_timeout = page_default_timeout
        self._base_results_dir = base_results_dir or settings.results_dir
        self.redis = aioredis.from_url(
            redis_url,
            password=settings.redis_passwd,
            encoding="utf-8",
            decode_responses=True,
        )
        self.interval = interval
        self.logger = logging.getLogger(self.name or "default")
        self.init_configurable()
        self.configurator = RedisConfigurator(self)

    def init_configurable(self):
        name_to_cfg = {cfg.name: cfg for cfg in self.configurable}
        for cfg in self.get_configurable(filter_=ConfigFilter.READ_ONLY):
            if cfg.depend_on and cfg.depend_on in name_to_cfg:
                name_to_cfg[cfg.depend_on]._updates.append(cfg)

    @property
    def personal_key(self):
        return f"{self.redis_key_prefix}:{self.people}"

    @property
    def person_page_url(self):
        return default.person_page_url.format(people=self.people)

    @property
    def status_key(self):
        return f"{self.redis_key_prefix}:{self.name}:status"

    async def get_status(self) -> WorkStatus:
        return WorkStatus(await self.redis.get(self.status_key) or WorkStatus.WAITING)

    async def set_status(self, status: WorkStatus):
        return await self.redis.set(self.status_key, status.value)

    async def get_state_path_from_redis(self) -> pathlib.Path | None:
        path = await self.redis.get(self.state_path_key)
        return pathlib.Path(path) if path else None

    async def set_state_path_to_redis(self, path: str | pathlib.Path):
        await self.redis.set(self.state_path_key, str(path))

    async def get_state_path(self) -> pathlib.Path | str:
        return await self.get_state_path_from_redis() or self.init_state_path

    @property
    def configs_key(self):
        return f"{self.redis_key_prefix}:{self.name}:configs"

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

    async def push_task(self, task: ArchiveTask):
        return await self.redis.rpush(self.tasks_key, task.as_value())

    async def pop_task(self) -> ArchiveTask | None:
        task = await self.redis.lpop(self.tasks_key)
        if task:
            return ArchiveTask.from_value(task)

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
    ) -> BrowserContext:
        state_path = await self.get_state_path()
        self.logger.info(f"Currently used state path: {state_path}")
        async with get_context(
            playwright,
            state_path,
            state_auto_save,
            browser_headless,
            init=self.init_context,
            **context_extra,
        ) as context:
            yield context

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

    @property
    def pause_key(self):
        return f"{self.redis_key_prefix}:{self.name}:pause"

    async def pause(self):
        return await self.redis.set(self.pause_key, 1)

    async def resume(self):
        return await self.redis.set(self.pause_key, 0)

    async def need_pause(self) -> bool:
        return int(await self.redis.get(self.pause_key) or 1) == 1

    async def _run(self, playwright, headless=True, **context_extra):
        raise NotImplementedError

    async def before_run(self):
        self.logger.debug("Before run")
        await self.configurator.load_to_worker()

    async def after_run(self):
        self.logger.debug("After run")
        self.logger.debug("Write all configs to redis")
        await self.configurator.sync_from_worker()

    @contextlib.asynccontextmanager
    async def rotate(self):
        if await self.need_pause():
            self.logger.info(f"{self.name} pausing")
            while await self.need_pause():
                await asyncio.sleep(1)
            self.logger.info(f"{self.name} resumed")
        await self.before_run()
        await self.set_status(WorkStatus.RUNNING)
        yield
        await self.set_status(WorkStatus.WAITING)
        await self.after_run()
        await asyncio.sleep(self.interval)

    async def run(
        self,
        headless=True,
        **context_extra,
    ):
        self.logger.info(f"{self.name} started.")
        await self.configurator.load_to_worker()
        while True:
            async with async_playwright() as playwright:
                async with self.rotate():
                    try:
                        self.logger.debug(f"{self.name}: New loop")
                        await self._run(playwright, headless, **context_extra)
                    except AbnormalError as e:
                        self.logger.error(e)
                        await self.handle_abnormal()
                    except Exception as e:
                        self.logger.exception(e)
