import json
import re
from datetime import datetime
from typing import Any
from urllib import parse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

from archive.core.profile import (
    clean_profile_text,
    optional_int,
    timestamp_to_datetime,
)

QUESTION_PATH_PATTERN = re.compile(r"^/question/([1-9]\d*)/?$")
QUESTION_INITIAL_DATA_SELECTOR = "#js-initialData"
QUESTION_INITIAL_DATA_MAX_CHARS = 5_000_000
QUESTION_DETAIL_HTML_MAX_CHARS = 50_000
QUESTION_DETAIL_TEXT_MAX_CHARS = 20_000
QUESTION_TITLE_MAX_CHARS = 300
QUESTION_TOPIC_MAX_ITEMS = 50


class QuestionReadError(RuntimeError):
    """表示知乎问题没有成功读取。"""


class QuestionTopic(BaseModel):
    """表示知乎问题关联的一个话题。"""

    id: str
    name: str
    url: str


class QuestionResult(BaseModel):
    """表示 MCP 返回的一条知乎问题。"""

    id: str
    title: str
    url: str
    detail: str
    detail_html: str
    author: str
    author_url: str
    topics: list[QuestionTopic]
    created_at: datetime | None
    updated_at: datetime | None
    answer_count: int | None
    follower_count: int | None
    visit_count: int | None
    comment_count: int | None


def parse_question_url(url: str) -> tuple[str, str]:
    """校验并标准化知乎问题链接。

    Args:
        url: 仅包含问题 ID、不包含回答 ID 的知乎问题链接。

    Returns:
        标准化后的 HTTPS 问题链接及问题 ID。
    """
    value = url.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("/question/"):
        value = f"https://www.zhihu.com{value}"
    parsed = parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("请输入完整的知乎问题链接")
    if parsed.username or parsed.password:
        raise ValueError("链接中不能包含用户认证信息")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("链接端口格式不正确") from error
    if port not in {None, 80, 443}:
        raise ValueError("链接不能包含自定义端口")
    if (parsed.hostname or "").lower() not in {"zhihu.com", "www.zhihu.com"}:
        raise ValueError("仅支持知乎问题链接")
    matched = QUESTION_PATH_PATTERN.fullmatch(parsed.path)
    if matched is None:
        raise ValueError("仅支持不包含回答 ID 的知乎问题链接")
    question_id = matched.group(1)
    if len(question_id) > 30:
        raise ValueError("知乎问题 ID 格式不正确")
    return f"https://www.zhihu.com/question/{question_id}", question_id


def truncate_question_html(value: Any) -> str:
    """限制问题 HTML 描述的最大返回长度。

    Args:
        value: 知乎响应中的问题 HTML 描述。
    """
    if not isinstance(value, str):
        return ""
    if len(value) <= QUESTION_DETAIL_HTML_MAX_CHARS:
        return value
    return f"{value[: QUESTION_DETAIL_HTML_MAX_CHARS - 1]}…"


def parse_question_topics(value: Any) -> list[QuestionTopic]:
    """把知乎话题数组转换为受控结构。

    Args:
        value: 知乎响应中的 topics 字段。
    """
    if not isinstance(value, list):
        return []
    topics: list[QuestionTopic] = []
    for raw_topic in value[:QUESTION_TOPIC_MAX_ITEMS]:
        if not isinstance(raw_topic, dict):
            continue
        topic_id = str(raw_topic.get("id") or "")
        name = clean_profile_text(
            raw_topic.get("name"),
            QUESTION_TITLE_MAX_CHARS,
        )
        if not topic_id and not name:
            continue
        topics.append(
            QuestionTopic(
                id=topic_id,
                name=name,
                url=(f"https://www.zhihu.com/topic/{topic_id}/hot" if topic_id else ""),
            )
        )
    return topics


def get_question_field(payload: dict[str, Any], snake: str, camel: str) -> Any:
    """兼容知乎问题对象中的蛇形与驼峰字段。

    Args:
        payload: 知乎问题对象。
        snake: JSON API 使用的蛇形字段名。
        camel: 页面初始化数据使用的驼峰字段名。
    """
    value = payload.get(snake)
    return payload.get(camel) if value is None else value


def extract_question_author(payload: dict[str, Any]) -> tuple[str, str]:
    """从页面初始化问题对象中提取作者及个人页地址。

    Args:
        payload: 知乎问题对象。
    """
    author = payload.get("author")
    if not isinstance(author, dict):
        return "", ""
    name = clean_profile_text(author.get("name"), QUESTION_TITLE_MAX_CHARS)
    token = str(author.get("url_token") or author.get("urlToken") or "").strip()
    if not token:
        return name, ""
    is_org = (
        author.get("is_org") is True
        or author.get("isOrg") is True
        or author.get("userType") == "organization"
    )
    author_type = "org" if is_org else "people"
    return (
        name,
        f"https://www.zhihu.com/{author_type}/{parse.quote(token, safe='._-~')}",
    )


def parse_question_payload(payload: dict[str, Any], question_id: str) -> QuestionResult:
    """把知乎问题对象转换为 MCP 结构化结果。

    Args:
        payload: 页面初始化数据中的知乎问题对象。
        question_id: 请求 URL 中经过校验的问题 ID。
    """
    author, author_url = extract_question_author(payload)
    detail_html = truncate_question_html(payload.get("detail"))
    detail = clean_profile_text(
        detail_html or payload.get("excerpt"),
        QUESTION_DETAIL_TEXT_MAX_CHARS,
    )
    return QuestionResult(
        id=question_id,
        title=clean_profile_text(payload.get("title"), QUESTION_TITLE_MAX_CHARS),
        url=f"https://www.zhihu.com/question/{question_id}",
        detail=detail,
        detail_html=detail_html,
        author=author,
        author_url=author_url,
        topics=parse_question_topics(payload.get("topics")),
        created_at=timestamp_to_datetime(payload.get("created")),
        updated_at=timestamp_to_datetime(
            get_question_field(payload, "updated_time", "updatedTime")
            or payload.get("updated")
        ),
        answer_count=optional_int(
            get_question_field(payload, "answer_count", "answerCount")
        ),
        follower_count=optional_int(
            get_question_field(payload, "follower_count", "followerCount")
        ),
        visit_count=optional_int(
            get_question_field(payload, "visit_count", "visitCount")
        ),
        comment_count=optional_int(
            get_question_field(payload, "comment_count", "commentCount")
        ),
    )


def parse_question_initial_data(raw: str, question_id: str) -> QuestionResult:
    """从知乎页面初始化 JSON 中提取指定问题。

    Args:
        raw: `#js-initialData` 脚本的 JSON 文本。
        question_id: 请求 URL 中经过校验的问题 ID。
    """
    if not raw.strip():
        raise QuestionReadError("知乎问题页初始化数据为空")
    if len(raw) > QUESTION_INITIAL_DATA_MAX_CHARS:
        raise QuestionReadError("知乎问题页初始化数据过大")
    try:
        initial_data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise QuestionReadError("知乎问题页初始化数据不是有效 JSON") from error
    if not isinstance(initial_data, dict):
        raise QuestionReadError("知乎问题页初始化数据结构异常")

    initial_state = initial_data.get("initialState")
    entities = (
        initial_state.get("entities") if isinstance(initial_state, dict) else None
    )
    questions = entities.get("questions") if isinstance(entities, dict) else None
    payload = questions.get(question_id) if isinstance(questions, dict) else None
    if not isinstance(payload, dict):
        raise QuestionReadError("知乎问题不存在、已被删除或无权查看")
    return parse_question_payload(payload, question_id)


async def read_question(
    page: Page,
    url: str,
    *,
    timeout: int = 30_000,
) -> QuestionResult:
    """从已打开的知乎问题页读取服务端注入的初始化数据。

    Args:
        page: 已导航到目标问题的 Playwright 页面。
        url: 已由 MCP 调用方提供的知乎问题链接。
        timeout: 等待页面初始化数据出现的毫秒超时。
    """
    _normalized_url, question_id = parse_question_url(url)
    locator = page.locator(QUESTION_INITIAL_DATA_SELECTOR)
    try:
        await locator.wait_for(state="attached", timeout=timeout)
        raw = await locator.text_content(timeout=timeout)
    except PlaywrightTimeoutError as error:
        raise QuestionReadError(
            "知乎问题页未返回初始化数据，可能页面不存在或触发访问风控"
        ) from error
    except PlaywrightError as error:
        raise QuestionReadError("读取知乎问题失败") from error
    return parse_question_initial_data(raw or "", question_id)
