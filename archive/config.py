import pathlib
from enum import Enum
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict


class default:  # noqa
    person_page_url = "https://www.zhihu.com/people/{people}"

    activity_item_selector = "div.Profile-main div.List-item"
    target_selector = "div.ContentItem"
    target_link_selector = "div.ContentItem h2 a[target=_blank]"
    state_file = "zhihu.state.json"


class Browser(str, Enum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    debug: bool = False
    secret_key: str = (
        "unsafe development secret key, please change me"  # 请生成一个随机字符串
    )
    root_dir: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
    results_dir: pathlib.Path = root_dir.joinpath(
        "results"
    )  # 结果存储目录，默认为：项目目录/results
    states_dir: pathlib.Path = root_dir.joinpath(
        "states"
    )  # 浏览器上下文state目录，默认为：项目目录/states
    people: str = "someone"  # 知乎用户，https://www.zhihu.com/people/<someone>
    activity_item_selector: str = default.activity_item_selector
    context_default_timeout: int = 10 * 1000  # 10s
    algorithm: str = "HS256"
    # redis配置
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_passwd: str | None = None
    # 是否使用无头模式
    archiver_headless: bool = True
    monitor_headless: bool = True
    login_worker_headless: bool = True
    log_level: Annotated[
        str,
        StringConstraints(to_upper=True),
    ] = "INFO"
    log_dir: pathlib.Path = root_dir.joinpath("logs")
    browser: Browser = Browser.CHROMIUM
    monitor_fetch_until: int = 1  # days，Monitor运行时默认抓取到1天前的动态
    monitor_interval: int = 60 * 5  # seconds，Monitor默认每5分钟检查一次新的动态
    screenshot_max_page_scroll_height: int = (
        0  # 截图允许的页面的最大高度，像素值。0表示不限制
    )

    @property
    def redis_url(self):
        return f"redis://{self.redis_host}:{self.redis_port}"


class APISettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".apienv", extra="ignore")

    # API认证账号
    enable_auth: bool = False
    username: str = "admin"
    password: str = "admin123456"
    cookies_max_age: int = 60 * 60 * 24 * 30
    # 允许携带 Cookie 访问 API 的跨域来源，多个来源使用英文逗号分隔
    cors_allowed_origins: str = ""

    @property
    def cors_origins(self) -> list[str]:
        """解析并校验允许访问 API 的跨域来源列表。"""
        origins: list[str] = []
        for value in self.cors_allowed_origins.split(","):
            origin = value.strip()
            if not origin:
                continue
            if origin == "*":
                raise ValueError("cors_allowed_origins 不允许使用通配符 *")
            parsed = urlsplit(origin)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "cors_allowed_origins 必须是完整的 HTTP(S) Origin，不能包含路径"
                )
            normalized_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            if normalized_origin not in origins:
                origins.append(normalized_origin)
        return origins


settings = Settings()
api_settings = APISettings()
