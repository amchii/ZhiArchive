import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import archive.api.app as app_module
from archive.api import security
from archive.core.archiver import ArchiveQueueService, TextArchive
from archive.core.base import TargetType
from archive.core.hot import HotQuestion, HotQuestionList
from archive.core.profile import ProfileContentType, ProfilePage
from archive.core.question import QuestionResult
from archive.mcp_server import (
    MCPBearerAuthMiddleware,
    create_mcp_server,
    enqueue_zhihu_archive,
    get_zhihu_archive_screenshot,
    get_zhihu_archive_task,
    get_zhihu_archiver_status,
    get_zhihu_login_qrcode,
    list_zhihu_collection_items,
    list_zhihu_hot_questions,
    list_zhihu_profile_items,
    read_zhihu_archive_artifact,
    read_zhihu_content,
    read_zhihu_question,
    resume_zhihu_archiver,
)
from archive.services import AppServices
from archive.storage import SQLiteStore


@pytest.mark.asyncio
async def test_mcp_exposes_expected_zhihu_tools() -> None:
    """验证一个 MCP Server 同时暴露读取、归档和登录能力。"""
    server = create_mcp_server()
    tools = {tool.name for tool in await server.list_tools()}

    assert tools == {
        "read_zhihu_content",
        "read_zhihu_question",
        "list_zhihu_hot_questions",
        "list_zhihu_profile_items",
        "list_zhihu_collection_items",
        "get_zhihu_auth_status",
        "get_zhihu_archiver_status",
        "resume_zhihu_archiver",
        "enqueue_zhihu_archive",
        "get_zhihu_archive_task",
        "read_zhihu_archive_artifact",
        "get_zhihu_archive_screenshot",
        "start_zhihu_login",
        "get_zhihu_login_status",
        "get_zhihu_login_qrcode",
    }


@pytest.mark.asyncio
async def test_read_content_applies_main_service_pagination(monkeypatch) -> None:
    """验证 MCP 正文分页上限来自主服务配置。"""
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={
            "reader_timeout_seconds": 30,
            "max_content_chars": 4,
        }
    )
    services.reader.submit = AsyncMock(
        return_value=TextArchive(
            title="标题",
            url="https://www.zhihu.com/question/1/answer/2",
            author="作者",
            author_url="",
            published_at="",
            updated_at="",
            target_type="回答",
            html="<p>abcdef</p>",
            markdown="abcdefghij",
        )
    )
    services.ensure_reader_started = AsyncMock()
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await read_zhihu_content(
        "https://www.zhihu.com/question/1/answer/2",
        offset=2,
    )

    assert result.content == "cdef"
    assert result.total_chars == 10
    assert result.truncated is True
    assert result.next_offset == 6
    services.reader.submit.assert_awaited_once_with(
        "https://www.zhihu.com/question/1/answer/2",
        timeout=30,
    )
    services.ensure_reader_started.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_read_question_validates_url_and_uses_reader(monkeypatch) -> None:
    """验证问题工具标准化链接并复用 Reader 超时配置。"""
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={"reader_timeout_seconds": 30}
    )
    services.ensure_reader_started = AsyncMock()
    expected = QuestionResult(
        id="123",
        title="测试问题",
        url="https://www.zhihu.com/question/123",
        detail="问题描述",
        detail_html="<p>问题描述</p>",
        author="提问者",
        author_url="",
        topics=[],
        created_at=None,
        updated_at=None,
        answer_count=1,
        follower_count=2,
        visit_count=3,
        comment_count=4,
    )
    services.reader.submit_question = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await read_zhihu_question(
        "https://www.zhihu.com/question/123?utm_source=test"
    )

    assert result is expected
    services.reader.submit_question.assert_awaited_once_with(
        "https://www.zhihu.com/question/123",
        timeout=30,
    )
    services.ensure_reader_started.assert_awaited_once_with()

    with pytest.raises(ToolError, match="不包含回答 ID"):
        await read_zhihu_question("https://www.zhihu.com/question/123/answer/456")
    assert services.reader.submit_question.await_count == 1


@pytest.mark.asyncio
async def test_list_hot_questions_validates_limit_and_uses_reader(monkeypatch) -> None:
    """验证热榜工具限制为三十条并复用 Reader 超时配置。"""
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={"reader_timeout_seconds": 30}
    )
    services.ensure_reader_started = AsyncMock()
    expected = HotQuestionList(
        items=[
            HotQuestion(
                rank=1,
                id="123",
                title="测试热榜问题",
                excerpt="问题摘要",
                url="https://www.zhihu.com/question/123",
                heat="100 万热度",
                answer_count=10,
                image_url=None,
                label=None,
                trend=0,
            )
        ],
        total=1,
        limit=10,
        fetched_at=datetime.now(timezone.utc),
    )
    services.reader.submit_hot_questions = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await list_zhihu_hot_questions(limit=10)

    assert result is expected
    services.reader.submit_hot_questions.assert_awaited_once_with(10, timeout=30)
    services.ensure_reader_started.assert_awaited_once_with()

    with pytest.raises(ToolError, match="1 到 30"):
        await list_zhihu_hot_questions(limit=31)  # type: ignore[arg-type]
    assert services.reader.submit_hot_questions.await_count == 1


@pytest.mark.asyncio
async def test_list_profile_items_uses_current_people_and_reader(monkeypatch) -> None:
    """验证个人列表默认读取全局用户并复用 Reader 超时配置。"""
    services = MagicMock()
    services.store.get_settings = AsyncMock(return_value={"people": "target-user"})
    services.mcp_config.get_config = AsyncMock(
        return_value={"reader_timeout_seconds": 45}
    )
    services.ensure_reader_started = AsyncMock()
    expected = ProfilePage(
        people="target-user",
        content_type=ProfileContentType.ANSWER,
        items=[],
        offset=20,
        limit=10,
        total=20,
        has_more=False,
        next_cursor=None,
    )
    services.reader.submit_profile = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await list_zhihu_profile_items(
        "answer",
        cursor="20",
        limit=10,
    )

    assert result is expected
    services.store.get_settings.assert_awaited_once_with("global")
    services.reader.submit_profile.assert_awaited_once_with(
        content_type=ProfileContentType.ANSWER,
        people="target-user",
        offset=20,
        limit=10,
        collection_id=None,
        timeout=45,
    )
    services.ensure_reader_started.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_list_collection_items_validates_id_and_uses_reader(monkeypatch) -> None:
    """验证收藏夹内容工具只接受数字 ID 并复用 Reader。"""
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={"reader_timeout_seconds": 30}
    )
    services.ensure_reader_started = AsyncMock()
    expected = ProfilePage(
        people=None,
        content_type=ProfileContentType.COLLECTION_ITEM,
        items=[],
        offset=0,
        limit=20,
        total=0,
        has_more=False,
        next_cursor=None,
        collection_id="123",
    )
    services.reader.submit_profile = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await list_zhihu_collection_items("123")

    assert result is expected
    services.reader.submit_profile.assert_awaited_once_with(
        content_type=ProfileContentType.COLLECTION_ITEM,
        people=None,
        offset=0,
        limit=20,
        collection_id="123",
        timeout=30,
    )
    with pytest.raises(ToolError, match="收藏夹 ID"):
        await list_zhihu_collection_items("not-a-number")
    assert services.reader.submit_profile.await_count == 1


async def call_middleware(
    middleware: MCPBearerAuthMiddleware,
    authorization: bytes | None,
    *,
    client: tuple[str, int] = ("203.0.113.10", 50000),
    host: bytes = b"example.test:9090",
    origin: bytes | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[list[dict[str, object]], AsyncMock]:
    """调用一次 MCP 鉴权中间件并返回 ASGI 消息。

    Args:
        middleware: 待测试的认证中间件。
        authorization: Authorization 请求头内容。
        client: ASGI scope 中的客户端地址。
        host: Host 请求头。
        origin: 可选的 Origin 请求头。
        extra_headers: 需要附加的其他请求头。
    """
    headers = [(b"host", host)]
    if authorization is not None:
        headers.append((b"authorization", authorization))
    if origin is not None:
        headers.append((b"origin", origin))
    headers.extend(extra_headers or [])
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "client": client,
    }
    sent: list[dict[str, object]] = []
    receive = AsyncMock()

    async def send(message: dict[str, object]) -> None:
        """记录中间件返回的 ASGI 消息。"""
        sent.append(message)

    await middleware(scope, receive, send)  # type: ignore[arg-type]
    return sent, receive


@pytest.mark.asyncio
async def test_mcp_middleware_uses_main_service_token(monkeypatch) -> None:
    """验证 MCP 路径使用主服务管理的独立 Bearer Token。"""
    inner = AsyncMock()
    middleware = MCPBearerAuthMiddleware(inner)
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={
            "enabled": True,
            "allow_anonymous_local": True,
            "token_configured": True,
        }
    )
    services.mcp_config.verify_token = AsyncMock(
        side_effect=lambda token: token == "valid-token"
    )
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    rejected, _ = await call_middleware(middleware, b"Bearer invalid")
    assert rejected[0]["status"] == 401
    inner.assert_not_awaited()

    accepted, _ = await call_middleware(middleware, b"Bearer valid-token")
    assert accepted == []
    inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_middleware_requires_token_for_loopback_when_anonymous_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证关闭本机匿名访问后回环请求仍需 Bearer Token。"""
    inner = AsyncMock()
    middleware = MCPBearerAuthMiddleware(inner)
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={
            "enabled": True,
            "allow_anonymous_local": False,
            "token_configured": False,
        }
    )
    services.mcp_config.verify_token = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    sent, _receive = await call_middleware(
        middleware,
        None,
        client=("127.0.0.1", 50000),
        host=b"localhost:9090",
    )

    assert sent[0]["status"] == 401
    inner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "host", "origin"),
    [
        (("127.0.0.1", 50000), b"localhost:9090", None),
        (("::1", 50000), b"[::1]:9090", b"http://[::1]:9090"),
        (
            ("::ffff:127.0.0.1", 50000),
            b"127.0.0.1:9090",
            b"http://localhost:9090",
        ),
    ],
)
async def test_mcp_middleware_allows_direct_loopback_anonymous(
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[str, int],
    host: bytes,
    origin: bytes | None,
) -> None:
    """验证显式开启后 IPv4、IPv6 和映射回环请求可匿名访问。"""
    inner = AsyncMock()
    middleware = MCPBearerAuthMiddleware(inner)
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={
            "enabled": True,
            "allow_anonymous_local": True,
            "token_configured": False,
        }
    )
    services.mcp_config.verify_token = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    sent, _receive = await call_middleware(
        middleware,
        None,
        client=client,
        host=host,
        origin=origin,
    )

    assert sent == []
    inner.assert_awaited_once()
    services.mcp_config.verify_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "host", "origin", "extra_headers"),
    [
        (("192.0.2.10", 50000), b"localhost:9090", None, None),
        (("127.0.0.1", 50000), b"example.test:9090", None, None),
        (("127.0.0.1", 50000), b"localhost:9090/invalid", None, None),
        (
            ("127.0.0.1", 50000),
            b"localhost:9090",
            b"https://example.test",
            None,
        ),
        (
            ("127.0.0.1", 50000),
            b"localhost:9090",
            None,
            [(b"x-forwarded-for", b"127.0.0.1")],
        ),
    ],
)
async def test_mcp_middleware_rejects_non_direct_anonymous_requests(
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[str, int],
    host: bytes,
    origin: bytes | None,
    extra_headers: list[tuple[bytes, bytes]] | None,
) -> None:
    """验证远程、伪造 Host、外部 Origin 和代理请求不能匿名访问。"""
    inner = AsyncMock()
    middleware = MCPBearerAuthMiddleware(inner)
    services = MagicMock()
    services.mcp_config.get_config = AsyncMock(
        return_value={
            "enabled": True,
            "allow_anonymous_local": True,
            "token_configured": False,
        }
    )
    services.mcp_config.verify_token = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    sent, _receive = await call_middleware(
        middleware,
        None,
        client=client,
        host=host,
        origin=origin,
        extra_headers=extra_headers,
    )

    assert sent[0]["status"] == 401
    inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_archive_enqueue_does_not_use_live_archiver(monkeypatch) -> None:
    """验证 MCP 通过独立入队服务提交任务。"""
    services = MagicMock()
    item = {
        "id": "activity-id",
        "target": {
            "title": "",
            "link": "https://www.zhihu.com/question/1/answer/2",
            "author": "",
            "fetched_at": "2026-07-17T00:00:00",
        },
        "meta": {
            "action": "手动归档",
            "target_type": TargetType.ANSWER,
            "acted_at": "2026-07-17T00:00:00",
            "raw": [],
        },
        "people": "someone",
    }
    task = MagicMock(task_name="activity-id")
    services.archive_queue.enqueue_url = AsyncMock(return_value=(task, item))
    services.get_archiver_status = AsyncMock(
        return_value={
            "paused": True,
            "status": "waiting",
            "worker_alive": True,
            "last_error": None,
        }
    )
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await enqueue_zhihu_archive(item["target"]["link"])

    assert result.task_id == "activity-id"
    assert result.archiver.paused is True
    services.archive_queue.enqueue_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_reads_and_resumes_archiver_status(monkeypatch) -> None:
    """验证 MCP 可读取并恢复 Archiver 后台任务。"""
    services = MagicMock()
    services.get_archiver_status = AsyncMock(
        return_value={
            "paused": True,
            "status": "waiting",
            "worker_alive": True,
            "last_error": None,
        }
    )
    services.resume_archiver = AsyncMock(
        return_value={
            "paused": False,
            "status": "waiting",
            "worker_alive": True,
            "last_error": None,
        }
    )
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    current = await get_zhihu_archiver_status()
    resumed = await resume_zhihu_archiver()

    assert current.paused is True
    assert resumed.paused is False
    assert resumed.worker_alive is True
    services.resume_archiver.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_mcp_completed_archive_task_returns_artifacts(
    monkeypatch,
    tmp_path,
) -> None:
    """验证完成任务会返回归档目录和各产物文件路径。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.set_settings("global", {"people": "someone"})
    queue = ArchiveQueueService(store)
    task, _item = await queue.enqueue_url("https://www.zhihu.com/question/1/answer/2")
    archive_path = "someone/archives/answer"
    files = {
        "screenshot": "answer.jpeg",
        "info": "info.json",
        "markdown": "answer.md",
    }
    await store.mark_archive_task_done(
        task.task_name,
        {"archive_path": archive_path, "files": files},
    )
    services = MagicMock(store=store)
    services.get_archiver_status = AsyncMock(
        return_value={
            "paused": False,
            "status": "waiting",
            "worker_alive": True,
            "last_error": None,
        }
    )
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    result = await get_zhihu_archive_task(task.task_name)

    assert result.status == "done"
    assert result.archive_path == archive_path
    assert result.files == files
    await store.close()


@pytest.mark.asyncio
async def test_mcp_reads_archive_text_and_screenshot(
    monkeypatch,
    tmp_path,
) -> None:
    """验证 MCP 通过任务索引分页读取文本并返回截图图片。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.set_settings("global", {"people": "someone"})
    queue = ArchiveQueueService(store)
    task, _item = await queue.enqueue_url("https://www.zhihu.com/question/1/answer/2")
    archive_path = "someone/archives/2026/08/11/example"
    archive_dir = tmp_path.joinpath(*archive_path.split("/"))
    archive_dir.mkdir(parents=True)
    archive_dir.joinpath("info.json").write_text(
        '{"title":"测试回答"}',
        encoding="utf-8",
    )
    archive_dir.joinpath("answer.md").write_text(
        "0123456789",
        encoding="utf-8",
    )
    archive_dir.joinpath("answer.html").write_text(
        "<p>测试回答</p>",
        encoding="utf-8",
    )
    archive_dir.joinpath("answer.jpeg").write_bytes(b"jpeg-data")
    await store.mark_archive_task_done(
        task.task_name,
        {
            "archive_path": archive_path,
            "files": {
                "info": "info.json",
                "markdown": "answer.md",
                "html": "answer.html",
                "screenshot": "answer.jpeg",
            },
        },
    )
    services = MagicMock(store=store)
    services.mcp_config.get_config = AsyncMock(return_value={"max_content_chars": 4})
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )
    monkeypatch.setattr(
        "archive.mcp_server.settings.results_dir",
        tmp_path,
    )

    text_result = await read_zhihu_archive_artifact(
        task.task_name,
        "markdown",
        offset=2,
    )
    screenshot = await get_zhihu_archive_screenshot(task.task_name)

    assert text_result.filename == "answer.md"
    assert text_result.content_format == "markdown"
    assert text_result.content == "2345"
    assert text_result.total_chars == 10
    assert text_result.next_offset == 6
    assert screenshot.data == b"jpeg-data"
    await store.close()


@pytest.mark.asyncio
async def test_mcp_archive_artifact_rejects_unsafe_or_missing_files(
    monkeypatch,
    tmp_path,
) -> None:
    """验证 MCP 不读取未完成任务、越界索引或已丢失产物。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.set_settings("global", {"people": "someone"})
    queue = ArchiveQueueService(store)
    task, _item = await queue.enqueue_url("https://www.zhihu.com/question/1/answer/2")
    services = MagicMock(store=store)
    services.mcp_config.get_config = AsyncMock(return_value={"max_content_chars": 100})
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )
    monkeypatch.setattr(
        "archive.mcp_server.settings.results_dir",
        tmp_path,
    )

    with pytest.raises(ToolError, match="尚未完成"):
        await read_zhihu_archive_artifact(task.task_name, "markdown")

    await store.mark_archive_task_done(
        task.task_name,
        {
            "archive_path": "someone/archives/example",
            "files": {"markdown": "../secret.md"},
        },
    )
    with pytest.raises(ToolError, match="索引不合法"):
        await read_zhihu_archive_artifact(task.task_name, "markdown")

    await store.mark_archive_task_done(
        task.task_name,
        {
            "archive_path": "someone/archives/example",
            "files": {"markdown": "missing.md"},
        },
    )
    with pytest.raises(ToolError, match="已丢失"):
        await read_zhihu_archive_artifact(task.task_name, "markdown")
    await store.close()


def test_mcp_initialize_uses_token_rotated_by_main_service(
    monkeypatch,
    tmp_path,
) -> None:
    """验证主服务轮换并启用 Token 后可完成 MCP initialize。"""
    services = AppServices(SQLiteStore(tmp_path / "zhi.sqlite3"))
    monkeypatch.setattr(app_module, "AppServices", lambda: services)
    monkeypatch.setattr(security.api_settings, "enable_auth", False)

    with TestClient(app_module.app, base_url="http://localhost:9090") as client:
        token_response = client.post("/zhi/core/mcp/token")
        assert token_response.status_code == 200
        token_payload = token_response.json()
        config_response = client.put(
            "/zhi/core/mcp/config",
            json={
                "enabled": True,
                "reader_timeout_seconds": token_payload["reader_timeout_seconds"],
                "max_content_chars": token_payload["max_content_chars"],
            },
        )
        assert config_response.status_code == 200
        assert services.reader_task is None

        initialize_response = client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token_payload['token']}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
        )
        tools_response = client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token_payload['token']}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )

    assert initialize_response.status_code == 200
    assert initialize_response.json()["result"]["serverInfo"]["name"] == ("ZhiArchive")
    assert tools_response.status_code == 200
    assert {tool["name"] for tool in tools_response.json()["result"]["tools"]} == {
        "read_zhihu_content",
        "read_zhihu_question",
        "list_zhihu_hot_questions",
        "list_zhihu_profile_items",
        "list_zhihu_collection_items",
        "get_zhihu_auth_status",
        "get_zhihu_archiver_status",
        "resume_zhihu_archiver",
        "enqueue_zhihu_archive",
        "get_zhihu_archive_task",
        "read_zhihu_archive_artifact",
        "get_zhihu_archive_screenshot",
        "start_zhihu_login",
        "get_zhihu_login_status",
        "get_zhihu_login_qrcode",
    }


def test_main_app_lifespan_can_restart(monkeypatch, tmp_path) -> None:
    """验证同一 FastAPI app 可在一个进程内重复启动生命周期。"""
    counter = 0

    def create_services() -> AppServices:
        """为每次应用生命周期创建独立 SQLite 服务容器。"""
        nonlocal counter
        counter += 1
        return AppServices(SQLiteStore(tmp_path / f"zhi-{counter}.sqlite3"))

    monkeypatch.setattr(app_module, "AppServices", create_services)
    monkeypatch.setattr(security.api_settings, "enable_auth", False)

    for _index in range(2):
        with TestClient(app_module.app, base_url="http://localhost:9090") as client:
            assert client.get("/healthz").status_code == 200


def test_reader_failure_does_not_mark_main_service_unhealthy(tmp_path) -> None:
    """验证可选 Reader 失败不会拖垮 Monitor 和 Archiver 健康状态。"""
    services = AppServices(SQLiteStore(tmp_path / "zhi.sqlite3"))
    services.worker_errors["reader"] = "driver exited"

    assert services.healthy() is True


@pytest.mark.asyncio
async def test_resume_archiver_restarts_finished_worker(tmp_path) -> None:
    """验证恢复 Archiver 时会重启已经结束的后台任务。"""
    services = AppServices(SQLiteStore(tmp_path / "zhi.sqlite3"))
    await services.store.connect()
    await services.store.seed_defaults()

    async def finish_worker() -> None:
        """模拟一个已经异常结束的 Archiver 顶层任务。"""

    finished_task = asyncio.create_task(finish_worker())
    await finished_task
    services.archiver_task = finished_task
    services.worker_errors["archiver"] = "worker exited"

    status = await services.resume_archiver()

    assert status["paused"] is False
    assert status["worker_alive"] is True
    assert services.archiver_task is not finished_task
    assert "archiver" not in services.worker_errors
    await services.stop()


@pytest.mark.asyncio
async def test_cancelled_reader_caller_does_not_start_duplicate_worker(
    tmp_path,
) -> None:
    """验证调用方取消等待后，后续请求会复用同一个 Reader 启动任务。"""
    services = AppServices(SQLiteStore(tmp_path / "zhi.sqlite3"))
    worker_started = asyncio.Event()
    allow_ready = asyncio.Event()
    run_count = 0

    async def run_reader(headless: bool = True) -> None:
        """模拟启动较慢且持续运行的 Reader。"""
        nonlocal run_count
        run_count += 1
        worker_started.set()
        try:
            await allow_ready.wait()
            services.reader._ready.set()
            await asyncio.Future()
        finally:
            services.reader._ready.clear()

    services.reader.run_reader = run_reader  # type: ignore[method-assign]
    first_call = asyncio.create_task(services.ensure_reader_started())
    await worker_started.wait()
    original_reader_task = services.reader_task

    first_call.cancel()
    await asyncio.gather(first_call, return_exceptions=True)
    second_call = asyncio.create_task(services.ensure_reader_started())
    allow_ready.set()
    await second_call

    assert run_count == 1
    assert services.reader_task is original_reader_task
    assert services.reader_task is not None
    services.reader_task.cancel()
    await asyncio.gather(services.reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_qrcode_tool_rejects_finished_login_task(
    monkeypatch,
    tmp_path,
) -> None:
    """验证 MCP 不会返回成功或失败任务遗留的旧二维码。"""
    qrcode_path = tmp_path / "finished.qrcode.png"
    qrcode_path.write_bytes(b"stale")
    services = MagicMock()
    services.store.get_login_task = AsyncMock(
        return_value={
            "id": "finished",
            "qrcode_path": str(qrcode_path),
            "status": "failed",
            "last_error": "timeout",
        }
    )
    monkeypatch.setattr(
        "archive.mcp_server.get_current_services",
        lambda: services,
    )

    with pytest.raises(ToolError, match="任务已结束"):
        await get_zhihu_login_qrcode("finished")
