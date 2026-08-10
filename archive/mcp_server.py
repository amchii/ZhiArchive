import json
import pathlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

import aiofiles
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from archive.config import settings
from archive.core.base import TargetType
from archive.core.profile import (
    ProfileContentType,
    ProfilePage,
    decode_profile_cursor,
    normalize_collection_id,
    normalize_people,
    validate_profile_limit,
)
from archive.core.question import QuestionResult, parse_question_url
from archive.services import get_current_services, new_qrcode_task

if TYPE_CHECKING:
    from archive.services import AppServices

MAX_QRCODE_BYTES = 1024 * 1024
ProfilePageLimit = Annotated[int, Field(ge=1, le=20)]
MCPToolFunction = Callable[..., Any]
MCPToolRegistration = tuple[
    MCPToolFunction,
    ToolAnnotations,
    bool | None,
]
MCP_TOOL_REGISTRATIONS: list[MCPToolRegistration] = []


class ZhihuContentResult(BaseModel):
    """表示 MCP 返回的一页知乎正文。"""

    title: str
    url: str
    author: str
    author_url: str
    published_at: str
    updated_at: str
    target_type: str
    content_format: Literal["markdown", "html"]
    content: str
    offset: int
    total_chars: int
    truncated: bool
    next_offset: int | None


class AuthStatusResult(BaseModel):
    """表示不包含 Cookie 的知乎登录态摘要。"""

    configured: bool
    valid: bool
    source: str | None
    updated_at: datetime | None
    cookie_count: int
    error: str | None


class ArchiveTaskResult(BaseModel):
    """表示 MCP 提交或查询到的归档任务。"""

    task_id: str
    url: str
    target_type: str
    status: str
    attempts: int = 0
    error: str | None = None


class LoginTaskResult(BaseModel):
    """表示二维码登录任务的公开状态。"""

    login_id: str
    status: str
    error: str | None = None


def require_services() -> "AppServices":
    """返回当前主服务容器，未就绪时抛出 MCP 工具错误。"""
    services = get_current_services()
    if services is None:
        raise ToolError("ZhiArchive 主服务尚未就绪")
    return services


async def send_http_error(
    send: Send,
    status: int,
    message: str,
    *,
    authenticate: bool = False,
) -> None:
    """发送不进入 MCP 协议层的 JSON 错误响应。

    Args:
        send: ASGI send callable。
        status: HTTP 状态码。
        message: 返回给调用方的错误消息。
        authenticate: 是否附加 Bearer 认证提示头。
    """
    body = json.dumps({"detail": message}, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def register_mcp_tool(
    *,
    annotations: ToolAnnotations,
    structured_output: bool | None = None,
) -> Callable[[MCPToolFunction], MCPToolFunction]:
    """记录工具函数及注册参数，供每次应用生命周期创建新 Server。

    Args:
        annotations: MCP 工具行为提示。
        structured_output: 是否启用结构化输出覆盖值。
    """

    def decorator(function: MCPToolFunction) -> MCPToolFunction:
        """保存单个工具注册信息并原样返回函数。"""
        MCP_TOOL_REGISTRATIONS.append((function, annotations, structured_output))
        return function

    return decorator


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def read_zhihu_content(
    url: str,
    content_format: Literal["markdown", "html"] = "markdown",
    offset: int = 0,
) -> ZhihuContentResult:
    """读取知乎回答或专栏文章，并按主服务限制返回一页正文。

    Args:
        url: 知乎回答或专栏文章链接。
        content_format: 返回 Markdown 或清理后的 HTML。
        offset: 长正文分页读取时的起始字符位置。
    """
    if offset < 0:
        raise ToolError("offset 不能小于 0")
    services = require_services()
    config = await services.mcp_config.get_config()
    try:
        await services.ensure_reader_started()
        archive = await services.reader.submit(
            url,
            timeout=config["reader_timeout_seconds"],
        )
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error

    full_content = archive[content_format]
    if offset > len(full_content):
        raise ToolError("offset 超出正文长度")
    end = min(offset + config["max_content_chars"], len(full_content))
    next_offset = end if end < len(full_content) else None
    return ZhihuContentResult(
        title=archive["title"],
        url=archive["url"],
        author=archive["author"],
        author_url=archive["author_url"],
        published_at=archive["published_at"],
        updated_at=archive["updated_at"],
        target_type=archive["target_type"],
        content_format=content_format,
        content=full_content[offset:end],
        offset=offset,
        total_chars=len(full_content),
        truncated=next_offset is not None,
        next_offset=next_offset,
    )


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def read_zhihu_question(url: str) -> QuestionResult:
    """读取一个知乎问题本身，不读取其下的回答正文。

    Args:
        url: `https://www.zhihu.com/question/{id}` 格式的知乎问题链接。
    """
    services = require_services()
    try:
        normalized_url, _question_id = parse_question_url(url)
        config = await services.mcp_config.get_config()
        await services.ensure_reader_started()
        return await services.reader.submit_question(
            normalized_url,
            timeout=config["reader_timeout_seconds"],
        )
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


async def resolve_profile_people(
    services: "AppServices",
    people: str | None,
) -> str:
    """解析 MCP 个人列表工具使用的知乎用户 ID。

    Args:
        services: 当前主服务容器。
        people: 调用方显式指定的用户 ID；为空时读取全局配置。
    """
    if people is not None:
        return normalize_people(people)
    configs = await services.store.get_settings("global")
    return normalize_people(str(configs.get("people") or settings.people))


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def list_zhihu_profile_items(
    content_type: Literal["answer", "article", "pin", "collection"],
    people: str | None = None,
    cursor: str | None = None,
    limit: ProfilePageLimit = 20,
) -> ProfilePage:
    """分页查看知乎用户发布的回答、文章、想法或收藏夹。

    Args:
        content_type: 列表类型：answer、article、pin 或 collection。
        people: 知乎个人主页 URL 中 `/people/` 后的标识；省略时使用全局目标用户。
        cursor: 上一页返回的 next_cursor；第一页省略。
        limit: 单页条目数，范围 1 到 20。
    """
    services = require_services()
    try:
        resolved_people = await resolve_profile_people(services, people)
        offset = decode_profile_cursor(cursor)
        validate_profile_limit(limit)
        profile_type = ProfileContentType(content_type)
        config = await services.mcp_config.get_config()
        await services.ensure_reader_started()
        return await services.reader.submit_profile(
            content_type=profile_type,
            people=resolved_people,
            offset=offset,
            limit=limit,
            collection_id=None,
            timeout=config["reader_timeout_seconds"],
        )
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def list_zhihu_collection_items(
    collection_id: str,
    cursor: str | None = None,
    limit: ProfilePageLimit = 20,
) -> ProfilePage:
    """分页查看一个知乎收藏夹中的内容。

    Args:
        collection_id: list_zhihu_profile_items 返回的收藏夹 ID。
        cursor: 上一页返回的 next_cursor；第一页省略。
        limit: 单页条目数，范围 1 到 20。
    """
    services = require_services()
    try:
        normalized_collection_id = normalize_collection_id(collection_id)
        offset = decode_profile_cursor(cursor)
        validate_profile_limit(limit)
        config = await services.mcp_config.get_config()
        await services.ensure_reader_started()
        return await services.reader.submit_profile(
            content_type=ProfileContentType.COLLECTION_ITEM,
            people=None,
            offset=offset,
            limit=limit,
            collection_id=normalized_collection_id,
            timeout=config["reader_timeout_seconds"],
        )
    except (RuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_zhihu_auth_status() -> AuthStatusResult:
    """读取当前托管知乎登录态摘要，不返回 Cookie 或文件路径。"""
    status = await require_services().auth_state.status()
    return AuthStatusResult(**status)


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def enqueue_zhihu_archive(url: str) -> ArchiveTaskResult:
    """把知乎回答或文章加入现有 Archiver 队列。

    Args:
        url: 知乎回答或专栏文章链接。
    """
    services = require_services()
    try:
        task, item = await services.archive_queue.enqueue_url(url)
    except ValueError as error:
        raise ToolError(str(error)) from error
    target_type = item["meta"]["target_type"]
    if isinstance(target_type, TargetType):
        target_type = target_type.value
    return ArchiveTaskResult(
        task_id=task.task_name,
        url=item["target"]["link"],
        target_type=str(target_type),
        status="pending",
    )


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_zhihu_archive_task(task_id: str) -> ArchiveTaskResult:
    """查询一条归档任务的状态。

    Args:
        task_id: enqueue_zhihu_archive 返回的任务 ID。
    """
    task = await require_services().store.get_archive_task(task_id)
    if task is None:
        raise ToolError("归档任务不存在")
    item = task["payload"]
    return ArchiveTaskResult(
        task_id=task["id"],
        url=str(item["target"]["link"]),
        target_type=str(item["meta"]["target_type"]),
        status=str(task["status"]),
        attempts=int(task["attempts"]),
        error=task["last_error"],
    )


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def start_zhihu_login() -> LoginTaskResult:
    """启动或复用当前二维码登录任务。"""
    services = require_services()
    task = await services.start_login_task(new_qrcode_task())
    status = await services.store.get_login_task_status(task.id)
    return LoginTaskResult(login_id=task.id, status=status)


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_zhihu_login_status(login_id: str) -> LoginTaskResult:
    """查询二维码登录任务状态。

    Args:
        login_id: start_zhihu_login 返回的登录任务 ID。
    """
    task = await require_services().store.get_login_task(login_id)
    if task is None:
        raise ToolError("登录任务不存在")
    return LoginTaskResult(
        login_id=task["id"],
        status=task["status"],
        error=task["last_error"],
    )


@register_mcp_tool(
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_zhihu_login_qrcode(login_id: str) -> Image:
    """读取二维码登录图片，图片生成前调用会返回未就绪错误。

    Args:
        login_id: start_zhihu_login 返回的登录任务 ID。
    """
    task = await require_services().store.get_login_task(login_id)
    if task is None:
        raise ToolError("登录任务不存在")
    if task["status"] not in {"pending", "waiting_for_scan"}:
        raise ToolError(f"登录任务已结束，当前状态：{task['status']}")
    qrcode_path = pathlib.Path(task["qrcode_path"])
    try:
        async with aiofiles.open(qrcode_path, "rb") as file_obj:
            data = await file_obj.read(MAX_QRCODE_BYTES + 1)
    except FileNotFoundError as error:
        raise ToolError("登录二维码尚未生成，请稍后重试") from error
    if len(data) > MAX_QRCODE_BYTES:
        raise ToolError("登录二维码文件异常")
    return Image(data=data, format="png")


class MCPBearerAuthMiddleware:
    """使用主服务管理的独立 Bearer Token 保护 MCP 子应用。"""

    def __init__(self, app: ASGIApp) -> None:
        """包装 MCP ASGI 子应用。

        Args:
            app: FastMCP 生成的 Streamable HTTP ASGI 应用。
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """在 HTTP 请求进入 MCP session manager 前完成动态鉴权。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        services = get_current_services()
        if services is None:
            await send_http_error(send, 503, "ZhiArchive 主服务尚未就绪")
            return
        config = await services.mcp_config.get_config()
        if not config["enabled"]:
            await send_http_error(send, 503, "MCP 服务未启用")
            return
        if not config["token_configured"]:
            await send_http_error(send, 503, "MCP Token 尚未配置")
            return

        authorization = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                authorization = value.decode("latin-1")
                break
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not await services.mcp_config.verify_token(
            token.strip()
        ):
            await send_http_error(
                send,
                401,
                "MCP Bearer Token 无效",
                authenticate=True,
            )
            return
        await self.app(scope, receive, send)


def create_mcp_server() -> FastMCP:
    """创建带完整知乎工具集的新 FastMCP Server。"""
    server = FastMCP(
        "ZhiArchive",
        instructions=(
            "使用已由 ZhiArchive 主服务托管的知乎登录态读取正文、问题和个人内容列表、"
            "提交归档任务，并按需发起二维码登录。正文读取支持知乎回答和专栏文章；"
            "问题读取返回问题描述、话题和统计；个人列表支持回答、文章、想法、"
            "收藏夹及收藏夹内容。"
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        # MCP 由强制 Bearer Token 保护，并与主服务共用可配置的 Host。
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    for function, annotations, structured_output in MCP_TOOL_REGISTRATIONS:
        server.add_tool(
            function,
            annotations=annotations,
            structured_output=structured_output,
        )
    return server


class MCPRuntimeApplication:
    """为每次主应用生命周期创建可重新启动的 MCP 子应用。"""

    def __init__(self) -> None:
        """创建尚未进入应用生命周期的动态 ASGI 代理。"""
        self._app: ASGIApp | None = None

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """启动新的 FastMCP session manager，并在退出时解除代理。"""
        if self._app is not None:
            raise RuntimeError("MCP runtime 已经启动")
        server = create_mcp_server()
        app = MCPBearerAuthMiddleware(server.streamable_http_app())
        async with server.session_manager.run():
            self._app = app
            try:
                yield
            finally:
                self._app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """把请求转发给当前生命周期内的 MCP 子应用。"""
        app = self._app
        if app is None:
            if scope["type"] == "http":
                await send_http_error(send, 503, "MCP 服务尚未就绪")
                return
            return
        await app(scope, receive, send)


mcp_runtime = MCPRuntimeApplication()
mcp_http_app: ASGIApp = mcp_runtime
