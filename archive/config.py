import pathlib
from enum import Enum

from pydantic import constr
from pydantic_settings import BaseSettings


class default:  # noqa
    person_page_url = "https://www.zhihu.com/people/{people}"

    activity_item_selector = "div.Profile-main div[role=list] div.List-item"
    target_selector = "div.ContentItem"
    target_link_selector = "div.ContentItem h2 a[target=_blank]"
    state_file = "zhihu.state.json"


class Browser(str, Enum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"


class Settings(BaseSettings):
    debug: bool = False
    secret_key: str = "unsafe secret key"
    root_dir: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
    results_dir: pathlib.Path = root_dir.joinpath("results")
    states_dir: pathlib.Path = root_dir.joinpath("states")
    people: str = "MarryMea"
    activity_item_selector: str = default.activity_item_selector
    person_page_url: str = default.person_page_url.format(people=people)
    context_default_timeout: int = 10 * 1000  # 10s
    algorithm: str = "HS256"
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_passwd: str | None = None
    archiver_headless: bool = True
    monitor_headless: bool = True
    log_level: constr(to_upper=True) = "INFO"
    log_dir: pathlib.Path = root_dir.joinpath("logs")
    browser: Browser = Browser.CHROMIUM

    class Config:
        env_file = ".env"

    @property
    def redis_url(self):
        return f"redis://{self.redis_host}:{self.redis_port}"


class APISettings(BaseSettings):
    username: str
    password: str
    cookies_max_age: int = 60 * 60 * 24 * 30

    class Config:
        env_file = ".apienv"


settings = Settings()
api_settings = APISettings()
