from datetime import datetime, timezone
from typing import Any

from playwright.async_api import APIRequestContext
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel

from archive.core.profile import (
    ProfileRateLimitError,
    clean_profile_text,
    normalize_image_url,
    optional_int,
    parse_retry_after,
)
from archive.core.question import parse_question_url

HOT_LIST_API_URL = (
    "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50&desktop=true"
)
HOT_LIST_REFERER = "https://www.zhihu.com/hot"
HOT_LIST_MAX_ITEMS = 30
HOT_TITLE_MAX_CHARS = 300
HOT_EXCERPT_MAX_CHARS = 1_200
HOT_METRIC_MAX_CHARS = 50
HOT_LABEL_MAX_CHARS = 20


class HotListReadError(RuntimeError):
    """表示知乎热榜没有成功读取。"""


class HotListPermissionError(HotListReadError):
    """表示登录态无权读取知乎热榜。"""


class HotQuestion(BaseModel):
    """表示知乎热榜中的一个问题。"""

    rank: int
    id: str
    title: str
    excerpt: str
    url: str
    heat: str
    answer_count: int | None
    image_url: str | None
    label: str | None
    trend: int | None


class HotQuestionList(BaseModel):
    """表示 MCP 返回的一次知乎热榜快照。"""

    items: list[HotQuestion]
    total: int
    limit: int
    fetched_at: datetime


def validate_hot_limit(limit: int) -> int:
    """校验热榜工具允许返回的最大条目数。

    Args:
        limit: 调用方希望返回的热榜条目数。
    """
    if not 1 <= limit <= HOT_LIST_MAX_ITEMS:
        raise ValueError(f"limit 必须在 1 到 {HOT_LIST_MAX_ITEMS} 之间")
    return limit


def get_hot_area(target: dict[str, Any], snake: str, camel: str) -> dict[str, Any]:
    """兼容热榜接口和页面初始化数据中的字段命名。

    Args:
        target: 热榜条目的 target 对象。
        snake: 热榜 API 使用的蛇形字段名。
        camel: 页面初始化数据使用的驼峰字段名。
    """
    area = target.get(snake)
    if not isinstance(area, dict):
        area = target.get(camel)
    return area if isinstance(area, dict) else {}


def extract_hot_question_url(item: dict[str, Any]) -> tuple[str, str] | None:
    """从热榜条目提取并校验问题 URL 与问题 ID。

    Args:
        item: 热榜 API 中的单个条目。
    """
    target = item.get("target")
    target = target if isinstance(target, dict) else {}
    target_id = str(target.get("id") or "")
    if target_id.isascii() and target_id.isdecimal() and 0 < len(target_id) <= 30:
        return f"https://www.zhihu.com/question/{target_id}", target_id

    link = target.get("link")
    link = link if isinstance(link, dict) else {}
    raw_url = link.get("url")
    if isinstance(raw_url, str):
        try:
            return parse_question_url(raw_url)
        except ValueError:
            pass

    card_id = str(item.get("card_id") or item.get("cardId") or "")
    question_id = card_id.removeprefix("Q_")
    if (
        card_id.startswith("Q_")
        and question_id.isascii()
        and question_id.isdecimal()
        and len(question_id) <= 30
    ):
        return f"https://www.zhihu.com/question/{question_id}", question_id
    return None


def parse_hot_question(item: dict[str, Any], rank: int) -> HotQuestion | None:
    """把一个知乎热榜对象转换为受控问题结构。

    Args:
        item: 热榜 API 中的单个条目。
        rank: 条目在当前热榜快照中的排名。
    """
    resolved = extract_hot_question_url(item)
    if resolved is None:
        return None
    url, question_id = resolved
    target = item.get("target")
    if not isinstance(target, dict):
        return None

    title_area = get_hot_area(target, "title_area", "titleArea")
    title = clean_profile_text(
        title_area.get("text") or target.get("title"),
        HOT_TITLE_MAX_CHARS,
    )
    if not title:
        return None
    excerpt_area = get_hot_area(target, "excerpt_area", "excerptArea")
    metrics_area = get_hot_area(target, "metrics_area", "metricsArea")
    image_area = get_hot_area(target, "image_area", "imageArea")
    label_area = get_hot_area(target, "label_area", "labelArea")
    feed_specific = item.get("feed_specific")
    if not isinstance(feed_specific, dict):
        feed_specific = item.get("feedSpecific")
    feed_specific = feed_specific if isinstance(feed_specific, dict) else {}
    label = clean_profile_text(label_area.get("text"), HOT_LABEL_MAX_CHARS)
    if not label and item.get("debut") is True:
        label = "新"
    children = item.get("children")
    children = children if isinstance(children, list) else []
    first_child = children[0] if children and isinstance(children[0], dict) else {}
    return HotQuestion(
        rank=rank,
        id=question_id,
        title=title,
        excerpt=clean_profile_text(
            excerpt_area.get("text") or target.get("excerpt"),
            HOT_EXCERPT_MAX_CHARS,
        ),
        url=url,
        heat=clean_profile_text(
            metrics_area.get("text") or item.get("detail_text"),
            HOT_METRIC_MAX_CHARS,
        ),
        answer_count=optional_int(
            feed_specific.get("answer_count"),
            feed_specific.get("answerCount"),
            target.get("answer_count"),
            target.get("answerCount"),
        ),
        image_url=normalize_image_url(
            image_area.get("url") or first_child.get("thumbnail")
        ),
        label=label or None,
        trend=optional_int(label_area.get("trend"), item.get("trend")),
    )


def parse_hot_list_payload(payload: dict[str, Any], limit: int) -> HotQuestionList:
    """把知乎热榜响应转换为最多三十条问题的快照。

    Args:
        payload: 知乎热榜 API 响应。
        limit: 调用方希望返回的条目数。
    """
    normalized_limit = validate_hot_limit(limit)
    data = payload.get("data")
    if not isinstance(data, list):
        raise HotListReadError("知乎热榜响应结构异常")
    items: list[HotQuestion] = []
    for rank, raw_item in enumerate(data[:HOT_LIST_MAX_ITEMS], start=1):
        if not isinstance(raw_item, dict):
            continue
        item = parse_hot_question(raw_item, rank)
        if item is not None:
            items.append(item)
    if data and not items:
        raise HotListReadError("知乎热榜响应中没有可识别的问题")
    return HotQuestionList(
        items=items[:normalized_limit],
        total=len(items),
        limit=normalized_limit,
        fetched_at=datetime.now(timezone.utc),
    )


async def read_hot_questions(
    request: APIRequestContext,
    *,
    limit: int = HOT_LIST_MAX_ITEMS,
    timeout: int = 30_000,
) -> HotQuestionList:
    """通过当前 BrowserContext 的登录态读取知乎热榜问题。

    Args:
        request: 与 BrowserContext 共享 Cookie 的请求客户端。
        limit: 返回的热榜条目数，最大为三十。
        timeout: 单次知乎请求的毫秒超时。
    """
    normalized_limit = validate_hot_limit(limit)
    try:
        response = await request.get(
            HOT_LIST_API_URL,
            headers={"Referer": HOT_LIST_REFERER},
            timeout=timeout,
        )
    except PlaywrightError as error:
        raise HotListReadError("读取知乎热榜失败") from error
    if response.status == 401:
        raise HotListPermissionError("知乎登录状态无权读取热榜")
    if response.status in {403, 429}:
        headers = response.headers
        retry_after = (
            parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))
            if isinstance(headers, dict)
            else None
        )
        message = (
            "知乎热榜请求过于频繁，已停止继续请求"
            if response.status == 429
            else "知乎拒绝读取热榜，可能触发访问风控"
        )
        raise ProfileRateLimitError(
            message,
            status=response.status,
            retry_after=retry_after,
        )
    if not response.ok:
        raise HotListReadError(f"知乎热榜请求失败：HTTP {response.status}")
    try:
        payload = await response.json()
    except (PlaywrightError, ValueError) as error:
        raise HotListReadError("知乎热榜响应不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise HotListReadError("知乎热榜响应结构异常")
    return parse_hot_list_payload(payload, normalized_limit)
