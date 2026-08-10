import html
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any
from urllib.parse import quote, urlencode

from playwright.async_api import APIRequestContext
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel

PROFILE_API_ROOT = "https://www.zhihu.com/api/v4"
PROFILE_PAGE_SIZE_MAX = 20
PROFILE_EXCERPT_MAX_CHARS = 800
PROFILE_TITLE_MAX_CHARS = 200
PROFILE_INCLUDE = "data[*].excerpt,voteup_count,comment_count"
PROFILE_REFERER_PATHS = {
    "answer": "answers",
    "article": "posts",
    "pin": "pins",
    "collection": "collections",
}


class ProfileContentType(str, Enum):
    """表示个人内容列表及收藏夹条目的类型。"""

    ANSWER = "answer"
    ARTICLE = "article"
    PIN = "pin"
    COLLECTION = "collection"
    COLLECTION_ITEM = "collection_item"


class ProfileReadError(RuntimeError):
    """表示知乎个人内容列表没有成功读取。"""


class ProfilePermissionError(ProfileReadError):
    """表示登录态无权读取指定的知乎个人内容。"""


class ProfileRateLimitError(ProfileReadError):
    """表示知乎拒绝或限制了个人内容列表请求。"""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        retry_after: float | None = None,
    ) -> None:
        """保存触发冷却策略所需的响应信息。

        Args:
            message: 面向调用方的错误说明。
            status: 知乎响应状态码。
            retry_after: 服务端建议等待的秒数。
        """
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ProfileItem(BaseModel):
    """表示经过清洗的一条个人内容或收藏夹。"""

    id: str
    content_type: str
    title: str
    excerpt: str
    url: str
    author: str
    author_url: str
    created_at: datetime | None
    updated_at: datetime | None
    collected_at: datetime | None = None
    voteup_count: int | None = None
    comment_count: int | None = None
    image_url: str | None = None
    item_count: int | None = None
    follower_count: int | None = None
    is_public: bool | None = None


class ProfilePage(BaseModel):
    """表示 MCP 返回的一页个人内容。"""

    people: str | None
    content_type: ProfileContentType
    items: list[ProfileItem]
    offset: int
    limit: int
    total: int | None
    has_more: bool
    next_cursor: str | None
    collection_id: str | None = None


def normalize_people(people: str) -> str:
    """校验并标准化知乎用户 URL 标识。

    Args:
        people: 知乎个人主页 `/people/` 后的标识。
    """
    value = people.strip()
    if not value:
        raise ValueError("知乎用户 ID 不能为空")
    if len(value) > 200 or any(char.isspace() for char in value):
        raise ValueError("知乎用户 ID 格式不正确")
    if any(char in value for char in ("/", "\\", "?", "#", "\x00")):
        raise ValueError("知乎用户 ID 格式不正确")
    return value


def normalize_collection_id(collection_id: str) -> str:
    """校验并标准化知乎收藏夹 ID。

    Args:
        collection_id: `list_zhihu_profile_items` 返回的收藏夹 ID。
    """
    value = collection_id.strip()
    if not value.isascii() or not value.isdecimal() or len(value) > 30:
        raise ValueError("知乎收藏夹 ID 格式不正确")
    return value


def decode_profile_cursor(cursor: str | None) -> int:
    """把 MCP 游标转换为非负 offset。

    Args:
        cursor: 上一页返回的十进制 `next_cursor`。
    """
    if cursor is None:
        return 0
    value = cursor.strip()
    if not value.isascii() or not value.isdecimal():
        raise ValueError("分页 cursor 格式不正确")
    offset = int(value)
    if offset > 10_000_000:
        raise ValueError("分页 cursor 超出允许范围")
    return offset


def validate_profile_limit(limit: int) -> int:
    """校验个人内容单页数量。

    Args:
        limit: MCP 请求的单页条目数。
    """
    if not 1 <= limit <= PROFILE_PAGE_SIZE_MAX:
        raise ValueError(f"limit 必须在 1 到 {PROFILE_PAGE_SIZE_MAX} 之间")
    return limit


def build_profile_api_url(
    content_type: ProfileContentType,
    people: str,
    offset: int,
    limit: int,
) -> str:
    """构造固定知乎域名下的个人列表请求。

    Args:
        content_type: 待读取的个人内容类型。
        people: 已校验的知乎用户 ID。
        offset: 当前分页偏移量。
        limit: 当前分页条目数。
    """
    token = quote(people, safe="._-~")
    params: dict[str, str | int] = {"offset": offset, "limit": limit}
    if content_type == ProfileContentType.ANSWER:
        path = f"members/{token}/answers"
        params.update(include=PROFILE_INCLUDE, sort_by="created")
    elif content_type == ProfileContentType.ARTICLE:
        path = f"members/{token}/articles"
        params.update(include=PROFILE_INCLUDE, sort_by="created")
    elif content_type == ProfileContentType.PIN:
        path = f"v2/pins/{token}/moments"
    elif content_type == ProfileContentType.COLLECTION:
        path = f"people/{token}/collections"
    else:
        raise ValueError("不支持的知乎个人内容类型")
    return f"{PROFILE_API_ROOT}/{path}?{urlencode(params)}"


def build_collection_items_api_url(
    collection_id: str,
    offset: int,
    limit: int,
) -> str:
    """构造固定知乎域名下的收藏夹内容请求。

    Args:
        collection_id: 已校验的收藏夹 ID。
        offset: 当前分页偏移量。
        limit: 当前分页条目数。
    """
    params = urlencode({"offset": offset, "limit": limit})
    return f"{PROFILE_API_ROOT}/collections/{collection_id}/items?{params}"


def build_profile_referer(
    content_type: ProfileContentType,
    *,
    people: str | None,
    collection_id: str | None = None,
) -> str:
    """根据列表类型构造知乎页面实际使用的动态 Referer。

    Args:
        content_type: 当前读取的个人内容类型。
        people: 已校验的知乎用户 ID。
        collection_id: 已校验的收藏夹 ID。
    """
    if content_type == ProfileContentType.COLLECTION_ITEM:
        normalized_collection_id = normalize_collection_id(collection_id or "")
        return f"https://www.zhihu.com/collection/{normalized_collection_id}"
    normalized_people = normalize_people(people or "")
    referer_path = PROFILE_REFERER_PATHS.get(content_type.value)
    if referer_path is None:
        raise ValueError("不支持的知乎个人内容类型")
    token = quote(normalized_people, safe="._-~")
    return f"https://www.zhihu.com/people/{token}/{referer_path}"


def clean_profile_text(value: Any, max_chars: int) -> str:
    """清理列表文本中的 HTML、零宽字符和连续空白。

    Args:
        value: 知乎响应中的原始文本。
        max_chars: 清理后允许返回的最大字符数。
    """
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(value))
    text = re.sub(r"\s+", " ", text.replace("\u200b", " ")).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}…"


def parse_retry_after(value: Any) -> float | None:
    """把 Retry-After 响应头转换为非负等待秒数。

    Args:
        value: Retry-After 秒数或 HTTP 日期。
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def optional_int(*values: Any) -> int | None:
    """返回候选值中的第一个整数。

    Args:
        values: 按优先级排列的知乎响应字段值。
    """
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def timestamp_to_datetime(value: Any) -> datetime | None:
    """把知乎 Unix 时间戳转换为 UTC 时间。

    Args:
        value: 知乎响应中的时间戳字段。
    """
    timestamp = optional_int(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def normalize_image_url(value: Any) -> str | None:
    """从字符串或缩略图对象中提取 HTTPS 图片地址。

    Args:
        value: 图片 URL 或包含 URL 的对象。
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("original_url")
    if not isinstance(value, str):
        return None
    if value.startswith("//"):
        value = f"https:{value}"
    return value if value.startswith("https://") else None


def extract_author(item: dict[str, Any]) -> tuple[str, str]:
    """从知乎列表条目提取作者名称和主页地址。

    Args:
        item: 单条知乎内容响应。
    """
    author = item.get("author")
    if not isinstance(author, dict):
        return "", ""
    name = clean_profile_text(author.get("name"), PROFILE_TITLE_MAX_CHARS)
    token = str(author.get("url_token") or "").strip()
    if not token:
        return name, ""
    author_type = "org" if author.get("is_org") is True else "people"
    return name, f"https://www.zhihu.com/{author_type}/{quote(token, safe='._-~')}"


def extract_reaction_statistics(item: dict[str, Any]) -> dict[str, Any]:
    """返回知乎新版 reaction 中的统计对象。

    Args:
        item: 单条知乎内容响应。
    """
    reaction = item.get("reaction")
    if not isinstance(reaction, dict):
        return {}
    statistics = reaction.get("statistics")
    return statistics if isinstance(statistics, dict) else {}


def parse_answer_item(
    item: dict[str, Any],
    collected_at: Any = None,
) -> ProfileItem:
    """把回答响应转换为统一的个人内容条目。

    Args:
        item: 知乎回答对象。
        collected_at: 收藏夹包装层提供的收藏时间。
    """
    question = item.get("question")
    question = question if isinstance(question, dict) else {}
    answer_id = str(item.get("id") or "")
    question_id = str(question.get("id") or "")
    author, author_url = extract_author(item)
    statistics = extract_reaction_statistics(item)
    url = (
        f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
        if question_id and answer_id
        else ""
    )
    return ProfileItem(
        id=answer_id,
        content_type=ProfileContentType.ANSWER.value,
        title=clean_profile_text(question.get("title"), PROFILE_TITLE_MAX_CHARS),
        excerpt=clean_profile_text(item.get("excerpt"), PROFILE_EXCERPT_MAX_CHARS),
        url=url,
        author=author,
        author_url=author_url,
        created_at=timestamp_to_datetime(item.get("created_time")),
        updated_at=timestamp_to_datetime(item.get("updated_time")),
        collected_at=timestamp_to_datetime(collected_at),
        voteup_count=optional_int(
            item.get("voteup_count"),
            statistics.get("up_vote_count"),
            statistics.get("like_count"),
        ),
        comment_count=optional_int(
            item.get("comment_count"),
            statistics.get("comment_count"),
        ),
        image_url=normalize_image_url(item.get("thumbnail")),
    )


def parse_article_item(
    item: dict[str, Any],
    collected_at: Any = None,
) -> ProfileItem:
    """把文章响应转换为统一的个人内容条目。

    Args:
        item: 知乎文章对象。
        collected_at: 收藏夹包装层提供的收藏时间。
    """
    article_id = str(item.get("id") or "")
    author, author_url = extract_author(item)
    statistics = extract_reaction_statistics(item)
    return ProfileItem(
        id=article_id,
        content_type=ProfileContentType.ARTICLE.value,
        title=clean_profile_text(item.get("title"), PROFILE_TITLE_MAX_CHARS),
        excerpt=clean_profile_text(item.get("excerpt"), PROFILE_EXCERPT_MAX_CHARS),
        url=(f"https://zhuanlan.zhihu.com/p/{article_id}" if article_id else ""),
        author=author,
        author_url=author_url,
        created_at=timestamp_to_datetime(
            item.get("created") or item.get("created_time")
        ),
        updated_at=timestamp_to_datetime(
            item.get("updated") or item.get("updated_time")
        ),
        collected_at=timestamp_to_datetime(collected_at),
        voteup_count=optional_int(
            item.get("voteup_count"),
            statistics.get("up_vote_count"),
            statistics.get("like_count"),
        ),
        comment_count=optional_int(
            item.get("comment_count"),
            statistics.get("comment_count"),
        ),
        image_url=normalize_image_url(item.get("image_url")),
    )


def parse_pin_item(
    item: dict[str, Any],
    collected_at: Any = None,
) -> ProfileItem:
    """把想法响应转换为统一的个人内容条目。

    Args:
        item: 知乎想法对象。
        collected_at: 收藏夹包装层提供的收藏时间。
    """
    pin_id = str(item.get("id") or "")
    author, author_url = extract_author(item)
    statistics = extract_reaction_statistics(item)
    raw_content = item.get("content")
    if not isinstance(raw_content, str):
        raw_content = item.get("content_html")
    excerpt = clean_profile_text(
        raw_content,
        PROFILE_EXCERPT_MAX_CHARS,
    )
    title = clean_profile_text(item.get("excerpt_title"), PROFILE_TITLE_MAX_CHARS)
    if not title:
        title = clean_profile_text(excerpt, PROFILE_TITLE_MAX_CHARS)
    return ProfileItem(
        id=pin_id,
        content_type=ProfileContentType.PIN.value,
        title=title,
        excerpt=excerpt,
        url=f"https://www.zhihu.com/pin/{pin_id}" if pin_id else "",
        author=author,
        author_url=author_url,
        created_at=timestamp_to_datetime(
            item.get("created") or item.get("created_time")
        ),
        updated_at=timestamp_to_datetime(
            item.get("updated") or item.get("updated_time")
        ),
        collected_at=timestamp_to_datetime(collected_at),
        voteup_count=optional_int(
            item.get("like_count"),
            statistics.get("up_vote_count"),
            statistics.get("like_count"),
        ),
        comment_count=optional_int(
            item.get("comment_count"),
            statistics.get("comment_count"),
        ),
    )


def parse_collection_item(item: dict[str, Any]) -> ProfileItem:
    """把收藏夹响应转换为统一的个人内容条目。

    Args:
        item: 知乎收藏夹对象。
    """
    collection_id = str(item.get("id") or "")
    creator = item.get("creator")
    creator = creator if isinstance(creator, dict) else {}
    author, author_url = extract_author({"author": creator})
    return ProfileItem(
        id=collection_id,
        content_type=ProfileContentType.COLLECTION.value,
        title=clean_profile_text(item.get("title"), PROFILE_TITLE_MAX_CHARS),
        excerpt=clean_profile_text(item.get("description"), PROFILE_EXCERPT_MAX_CHARS),
        url=(
            f"https://www.zhihu.com/collection/{collection_id}" if collection_id else ""
        ),
        author=author,
        author_url=author_url,
        created_at=timestamp_to_datetime(item.get("created_time")),
        updated_at=timestamp_to_datetime(item.get("updated_time")),
        voteup_count=optional_int(item.get("like_count")),
        comment_count=optional_int(item.get("comment_count")),
        item_count=optional_int(item.get("item_count"), item.get("answer_count")),
        follower_count=optional_int(item.get("follower_count")),
        is_public=(
            item.get("is_public") if isinstance(item.get("is_public"), bool) else None
        ),
    )


def parse_unknown_item(
    item: dict[str, Any],
    collected_at: Any = None,
) -> ProfileItem:
    """为暂未识别的收藏内容生成最小安全条目。

    Args:
        item: 未识别类型的知乎内容对象。
        collected_at: 收藏夹包装层提供的收藏时间。
    """
    item_id = str(item.get("id") or "")
    item_type = str(item.get("type") or "unknown")
    author, author_url = extract_author(item)
    title = clean_profile_text(
        item.get("title") or item.get("excerpt_title"),
        PROFILE_TITLE_MAX_CHARS,
    )
    excerpt = clean_profile_text(
        item.get("excerpt") or item.get("content"),
        PROFILE_EXCERPT_MAX_CHARS,
    )
    return ProfileItem(
        id=item_id,
        content_type=item_type,
        title=title or clean_profile_text(excerpt, PROFILE_TITLE_MAX_CHARS),
        excerpt=excerpt,
        url="",
        author=author,
        author_url=author_url,
        created_at=timestamp_to_datetime(
            item.get("created") or item.get("created_time")
        ),
        updated_at=timestamp_to_datetime(
            item.get("updated") or item.get("updated_time")
        ),
        collected_at=timestamp_to_datetime(collected_at),
        voteup_count=optional_int(item.get("voteup_count"), item.get("like_count")),
        comment_count=optional_int(item.get("comment_count")),
        image_url=normalize_image_url(item.get("image_url") or item.get("thumbnail")),
    )


def parse_profile_item(
    item: dict[str, Any],
    content_type: ProfileContentType,
) -> ProfileItem:
    """按请求类型把知乎列表对象转换为统一条目。

    Args:
        item: 知乎列表中的单个对象。
        content_type: 当前读取的列表类型。
    """
    if content_type == ProfileContentType.ANSWER:
        return parse_answer_item(item)
    if content_type == ProfileContentType.ARTICLE:
        return parse_article_item(item)
    if content_type == ProfileContentType.PIN:
        return parse_pin_item(item)
    if content_type == ProfileContentType.COLLECTION:
        return parse_collection_item(item)

    content = item.get("content")
    content = content if isinstance(content, dict) else {}
    collected_at = item.get("created")
    item_type = content.get("type")
    if item_type == ProfileContentType.ANSWER.value:
        return parse_answer_item(content, collected_at)
    if item_type == ProfileContentType.ARTICLE.value:
        return parse_article_item(content, collected_at)
    if item_type == ProfileContentType.PIN.value:
        return parse_pin_item(content, collected_at)
    return parse_unknown_item(content, collected_at)


async def read_profile_page(
    request: APIRequestContext,
    *,
    content_type: ProfileContentType,
    people: str | None,
    offset: int,
    limit: int,
    collection_id: str | None = None,
    timeout: int = 30_000,
) -> ProfilePage:
    """通过当前 BrowserContext 的登录态读取一页个人内容。

    Args:
        request: 与 BrowserContext 共享 Cookie 的请求客户端。
        content_type: 待读取的个人内容类型。
        people: 个人列表对应的知乎用户 ID。
        offset: 当前分页偏移量。
        limit: 当前分页条目数。
        collection_id: 收藏夹内容列表对应的收藏夹 ID。
        timeout: 单次知乎请求的毫秒超时。
    """
    validate_profile_limit(limit)
    if content_type == ProfileContentType.COLLECTION_ITEM:
        normalized_collection_id = normalize_collection_id(collection_id or "")
        url = build_collection_items_api_url(
            normalized_collection_id,
            offset,
            limit,
        )
        normalized_people = None
    else:
        normalized_collection_id = None
        normalized_people = normalize_people(people or "")
        url = build_profile_api_url(
            content_type,
            normalized_people,
            offset,
            limit,
        )
    referer = build_profile_referer(
        content_type,
        people=normalized_people,
        collection_id=normalized_collection_id,
    )
    try:
        response = await request.get(
            url,
            headers={"Referer": referer},
            timeout=timeout,
        )
    except PlaywrightError as error:
        raise ProfileReadError("读取知乎个人内容失败") from error
    if response.status == 401:
        raise ProfilePermissionError("知乎登录状态无权读取该个人内容")
    if response.status in {403, 429}:
        headers = response.headers
        retry_after = (
            parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))
            if isinstance(headers, dict)
            else None
        )
        if response.status == 429:
            message = "知乎个人内容请求过于频繁，已停止继续请求"
        else:
            message = "知乎拒绝读取个人内容，可能是权限不足或触发访问风控"
        raise ProfileRateLimitError(
            message,
            status=response.status,
            retry_after=retry_after,
        )
    if not response.ok:
        raise ProfileReadError(f"知乎个人内容请求失败：HTTP {response.status}")
    try:
        payload = await response.json()
    except (PlaywrightError, ValueError) as error:
        raise ProfileReadError("知乎个人内容响应不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise ProfileReadError("知乎个人内容响应结构异常")
    raw_items = payload.get("data")
    paging = payload.get("paging")
    if not isinstance(raw_items, list) or not isinstance(paging, dict):
        raise ProfileReadError("知乎个人内容响应结构异常")
    items = [
        parse_profile_item(item, content_type)
        for item in raw_items
        if isinstance(item, dict)
    ]
    is_end = bool(paging.get("is_end"))
    has_more = bool(items) and not is_end
    next_cursor = str(offset + limit) if has_more else None
    return ProfilePage(
        people=normalized_people,
        content_type=content_type,
        items=items,
        offset=offset,
        limit=limit,
        total=optional_int(paging.get("totals")),
        has_more=has_more,
        next_cursor=next_cursor,
        collection_id=normalized_collection_id,
    )
