from unittest.mock import AsyncMock, MagicMock

import pytest

from archive.core.profile import (
    ProfileContentType,
    ProfilePermissionError,
    ProfileRateLimitError,
    build_profile_api_url,
    build_profile_referer,
    decode_profile_cursor,
    normalize_collection_id,
    normalize_people,
    parse_article_item,
    parse_collection_item,
    parse_pin_item,
    parse_retry_after,
    read_profile_page,
    validate_profile_limit,
)


@pytest.mark.asyncio
async def test_read_profile_page_normalizes_answer_and_pagination() -> None:
    """验证回答列表使用固定知乎地址并返回受控游标。"""
    response = MagicMock(status=200, ok=True)
    response.json = AsyncMock(
        return_value={
            "data": [
                {
                    "id": 22,
                    "type": "answer",
                    "excerpt": "<b>回答</b> 摘要",
                    "created_time": 1_700_000_000,
                    "updated_time": 1_700_000_100,
                    "comment_count": 3,
                    "reaction": {"statistics": {"like_count": 8}},
                    "question": {"id": 11, "title": "测试问题"},
                    "author": {"name": "作者", "url_token": "author-id"},
                }
            ],
            "paging": {
                "is_end": False,
                "totals": 21,
                "next": (
                    "https://www.zhihu.com/api/v4/members/target-user/answers"
                    "?offset=20&limit=10"
                ),
            },
        }
    )
    request = MagicMock()
    request.get = AsyncMock(return_value=response)

    page = await read_profile_page(
        request,
        content_type=ProfileContentType.ANSWER,
        people="target-user",
        offset=10,
        limit=10,
    )

    assert page.next_cursor == "20"
    assert page.has_more is True
    assert page.total == 21
    assert page.items[0].url == "https://www.zhihu.com/question/11/answer/22"
    assert page.items[0].excerpt == "回答 摘要"
    assert page.items[0].voteup_count == 8
    requested_url = request.get.await_args.args[0]
    assert requested_url.startswith(
        "https://www.zhihu.com/api/v4/members/target-user/answers?"
    )
    assert "offset=10" in requested_url
    assert "limit=10" in requested_url
    assert request.get.await_args.kwargs["headers"] == {
        "Referer": "https://www.zhihu.com/people/target-user/answers"
    }


def test_profile_item_parsers_cover_article_pin_and_collection() -> None:
    """验证文章、想法和收藏夹被清洗成统一模型。"""
    article = parse_article_item(
        {
            "id": 12,
            "title": "文章标题",
            "excerpt": "<p>文章摘要</p>",
            "created": 1_700_000_000,
            "updated": 1_700_000_100,
            "voteup_count": 9,
            "comment_count": 2,
            "image_url": "https://pic.example/article.jpg",
            "author": {"name": "作者", "url_token": "author-id"},
        }
    )
    pin = parse_pin_item(
        {
            "id": 13,
            "content": [{"type": "text", "content": "块数组正文"}],
            "content_html": "<p>一条想法正文</p>",
            "created": 1_700_000_000,
            "like_count": 7,
            "comment_count": 1,
            "author": {"name": "作者", "url_token": "author-id"},
        }
    )
    collection = parse_collection_item(
        {
            "id": 14,
            "title": "收藏夹",
            "description": "说明",
            "created_time": 1_700_000_000,
            "updated_time": 1_700_000_100,
            "item_count": 6,
            "follower_count": 5,
            "is_public": True,
            "creator": {"name": "创建者", "url_token": "creator-id"},
        }
    )

    assert article.url == "https://zhuanlan.zhihu.com/p/12"
    assert article.author_url == "https://www.zhihu.com/people/author-id"
    assert pin.title == "一条想法正文"
    assert pin.excerpt == "一条想法正文"
    assert pin.url == "https://www.zhihu.com/pin/13"
    assert collection.url == "https://www.zhihu.com/collection/14"
    assert collection.item_count == 6
    assert collection.is_public is True


def test_profile_author_parser_supports_organization_accounts() -> None:
    """验证机构作者链接使用知乎 `/org/` 路径。"""
    article = parse_article_item(
        {
            "id": 15,
            "title": "机构文章",
            "author": {
                "name": "中国航天科工",
                "url_token": "zhong-guo-hang-tian-ke-gong",
                "is_org": True,
            },
        }
    )

    assert article.author == "中国航天科工"
    assert article.author_url == (
        "https://www.zhihu.com/org/zhong-guo-hang-tian-ke-gong"
    )


@pytest.mark.asyncio
async def test_collection_items_unwrap_nested_content() -> None:
    """验证收藏夹列表会解析包装层中的真实内容。"""
    response = MagicMock(status=200, ok=True)
    response.json = AsyncMock(
        return_value={
            "data": [
                {
                    "created": 1_700_000_000,
                    "content": {
                        "id": 22,
                        "type": "answer",
                        "excerpt": "收藏的回答",
                        "question": {"id": 11, "title": "收藏问题"},
                        "author": {"name": "回答者", "url_token": "answerer"},
                    },
                }
            ],
            "paging": {"is_end": True, "totals": 1},
        }
    )
    request = MagicMock()
    request.get = AsyncMock(return_value=response)

    page = await read_profile_page(
        request,
        content_type=ProfileContentType.COLLECTION_ITEM,
        people=None,
        offset=0,
        limit=20,
        collection_id="12345",
    )

    assert page.people is None
    assert page.collection_id == "12345"
    assert page.has_more is False
    assert page.next_cursor is None
    assert page.items[0].content_type == "answer"
    assert page.items[0].created_at is None
    assert page.items[0].collected_at is not None
    assert request.get.await_args.args[0] == (
        "https://www.zhihu.com/api/v4/collections/12345/items?offset=0&limit=20"
    )
    assert request.get.await_args.kwargs["headers"] == {
        "Referer": "https://www.zhihu.com/collection/12345"
    }


def test_profile_validators_reject_untrusted_values() -> None:
    """验证用户标识、收藏夹 ID、cursor 和 limit 的边界。"""
    assert normalize_people(" target-user ") == "target-user"
    assert normalize_collection_id("123") == "123"
    assert decode_profile_cursor(None) == 0
    assert decode_profile_cursor("20") == 20
    assert validate_profile_limit(20) == 20
    assert "sort_by=created" in build_profile_api_url(
        ProfileContentType.ARTICLE,
        "target-user",
        0,
        20,
    )
    assert (
        build_profile_referer(
            ProfileContentType.ARTICLE,
            people="target-user",
        )
        == "https://www.zhihu.com/people/target-user/posts"
    )
    assert (
        build_profile_referer(
            ProfileContentType.PIN,
            people="target-user",
        )
        == "https://www.zhihu.com/people/target-user/pins"
    )
    assert (
        build_profile_referer(
            ProfileContentType.COLLECTION,
            people="target-user",
        )
        == "https://www.zhihu.com/people/target-user/collections"
    )

    for people in ("", "bad/user", "bad user", "bad?user"):
        with pytest.raises(ValueError):
            normalize_people(people)
    for collection_id in ("", "abc", "1/2", "١٢٣"):
        with pytest.raises(ValueError):
            normalize_collection_id(collection_id)
    for cursor in ("-1", "next", "10000001", "٢٠"):
        with pytest.raises(ValueError):
            decode_profile_cursor(cursor)
    for limit in (0, 21):
        with pytest.raises(ValueError):
            validate_profile_limit(limit)


@pytest.mark.asyncio
async def test_read_profile_page_maps_authentication_error() -> None:
    """验证知乎登录态失效时返回明确的权限错误。"""
    response = MagicMock(status=401, ok=False)
    request = MagicMock()
    request.get = AsyncMock(return_value=response)

    with pytest.raises(ProfilePermissionError, match="无权"):
        await read_profile_page(
            request,
            content_type=ProfileContentType.PIN,
            people="target-user",
            offset=0,
            limit=20,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_read_profile_page_maps_rate_limit_error(status: int) -> None:
    """验证知乎拒绝或限流响应会携带冷却信息。"""
    response = MagicMock(
        status=status,
        ok=False,
        headers={"retry-after": "120"},
    )
    request = MagicMock()
    request.get = AsyncMock(return_value=response)

    with pytest.raises(ProfileRateLimitError) as error_info:
        await read_profile_page(
            request,
            content_type=ProfileContentType.PIN,
            people="target-user",
            offset=0,
            limit=20,
        )

    assert error_info.value.status == status
    assert error_info.value.retry_after == 120


def test_parse_retry_after_rejects_invalid_values() -> None:
    """验证异常 Retry-After 不会生成错误冷却时间。"""
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("invalid") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after(None) is None
