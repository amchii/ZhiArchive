import pathlib

from pydantic_settings import BaseSettings


class default:  # noqa
    person_page_url = "https://www.zhihu.com/people/{people}"

    activity_item_selector = "div.Profile-main div[role=list] div.List-item"
    target_selector = "div.ContentItem"
    target_link_selector = "div.ContentItem h2 a[target=_blank]"
    state_file = "zhihu.state.json"


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
    redis_url: str = "redis://127.0.0.1:6379"
    archiver_headless: bool = True
    monitor_headless: bool = True

    class Config:
        env_file = ".env"


class APISettings(BaseSettings):
    username: str
    password: str
    cookies_max_age: int = 60 * 60 * 24 * 30

    class Config:
        env_file = ".apienv"


settings = Settings()
api_settings = APISettings()
