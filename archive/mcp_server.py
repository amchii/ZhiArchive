import ipaddress
import json
import pathlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

import aiofiles
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from archive.config import settings
from archive.core.base import TargetType
from archive.core.hot import (
    HOT_LIST_MAX_ITEMS,
    HotQuestionList,
    validate_hot_limit,
)
from archive.core.profile import (
    ProfileContentType,
    ProfilePage,
    decode_profile_cursor,
    normalize_collection_id,
    normalize_people,
    validate_profile_limit,
)
from archive.core.question import QuestionResult, parse_question_url
from archive.result_files import (
    MAX_TEXT_PREVIEW_SIZE,
    ResultPathError,
    ResultPreviewType,
    get_preview_type,
    resolve_result_file_path,
)
from archive.services import get_current_services, new_qrcode_task

if TYPE_CHECKING:
    from archive.services import AppServices

MAX_QRCODE_BYTES = 1024 * 1024
MAX_ARCHIVE_IMAGE_BYTES = 20 * 1024 * 1024
PROXY_IDENTITY_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-real-ip",
    }
)
ProfilePageLimit = Annotated[int, Field(ge=1, le=20)]
HotListLimit = Annotated[int, Field(ge=1, le=HOT_LIST_MAX_ITEMS)]
ArchiveTextArtifact = Literal["info", "markdown", "html"]
ArchiveArtifact = Literal["info", "markdown", "html", "screenshot"]
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


class ArchiverStatusResult(BaseModel):
    """表示 Archiver 后台队列的暂停和运行状态。"""

    paused: bool
    status: str
    worker_alive: bool
    last_error: str | None = None


class ArchiveTaskResult(BaseModel):
    """表示 MCP 提交或查询到的归档任务。"""

    task_id: str
    url: str
    target_type: str
    status: str
    archiver: ArchiverStatusResult
    attempts: int = 0
    error: str | None = None
    archive_path: str | None = Field(
        default=None,
        description="相对于 ZhiArchive results 根目录的归档目录标识",
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="归档产物类型与文件名，不表示客户端可访问服务器文件系统",
    )


class ArchiveArtifactResult(BaseModel):
    """表示 MCP 分页返回的一份归档文本产物。"""

    task_id: str
    artifact: ArchiveTextArtifact
    filename: str
    content_format: Literal["json", "markdown", "html"]
    content: str
    offset: int
    total_chars: int
    truncated: bool
    next_offset: int | None


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


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def list_zhihu_hot_questions(
    limit: HotListLimit = HOT_LIST_MAX_ITEMS,
) -> HotQuestionList:
    """读取当前知乎热榜中的问题，最多返回三十条。

    Args:
        limit: 返回的热榜问题数量，范围 1 到 30。
    """
    services = require_services()
    try:
        normalized_limit = validate_hot_limit(limit)
        config = await services.mcp_config.get_config()
        await services.ensure_reader_started()
        return await services.reader.submit_hot_questions(
            normalized_limit,
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
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_zhihu_archiver_status() -> ArchiverStatusResult:
    """读取 Archiver 是否暂停、后台任务是否存活及当前工作状态。"""
    status = await require_services().get_archiver_status()
    return ArchiverStatusResult(**status)


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def resume_zhihu_archiver() -> ArchiverStatusResult:
    """恢复 Archiver 队列运行；后台任务异常退出时也会重新启动。"""
    status = await require_services().resume_archiver()
    return ArchiverStatusResult(**status)


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def enqueue_zhihu_archive(url: str) -> ArchiveTaskResult:
    """把知乎回答或文章加入队列，并返回当前 Archiver 运行状态。

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
        archiver=ArchiverStatusResult(**(await services.get_archiver_status())),
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
    services = require_services()
    task = await services.store.get_archive_task(task_id)
    if task is None:
        raise ToolError("归档任务不存在")
    item = task["payload"]
    result = task["result"] or {}
    return ArchiveTaskResult(
        task_id=task["id"],
        url=str(item["target"]["link"]),
        target_type=str(item["meta"]["target_type"]),
        status=str(task["status"]),
        attempts=int(task["attempts"]),
        error=task["last_error"],
        archive_path=result.get("archive_path"),
        files=result.get("files", {}),
        archiver=ArchiverStatusResult(**(await services.get_archiver_status())),
    )


async def resolve_archive_artifact(
    services: "AppServices",
    task_id: str,
    artifact: ArchiveArtifact,
) -> tuple[pathlib.Path, str]:
    """从完成任务中安全解析一份归档产物。

    Args:
        services: 当前主服务容器。
        task_id: 已完成的归档任务 ID。
        artifact: 允许读取的产物类型。

    Returns:
        通过结果目录安全校验的文件路径和文件名。
    """
    task = await services.store.get_archive_task(task_id)
    if task is None:
        raise ToolError("归档任务不存在")
    if task["status"] != "done":
        raise ToolError(f"归档任务尚未完成，当前状态：{task['status']}")
    result = task["result"]
    if not isinstance(result, dict):
        raise ToolError("该完成任务没有可用的归档产物索引")
    archive_path = result.get("archive_path")
    files = result.get("files")
    filename = files.get(artifact) if isinstance(files, dict) else None
    if not isinstance(archive_path, str) or not isinstance(filename, str):
        raise ToolError(f"归档任务没有 {artifact} 产物")
    filename_path = pathlib.PurePosixPath(filename)
    if (
        filename_path.is_absolute()
        or len(filename_path.parts) != 1
        or filename_path.name in {"", ".", ".."}
    ):
        raise ToolError("归档产物索引不合法")
    relative_path = pathlib.PurePosixPath(archive_path).joinpath(filename)
    try:
        target_path, normalized_path = resolve_result_file_path(
            relative_path.as_posix(),
            settings.results_dir,
        )
    except ResultPathError as error:
        raise ToolError("归档产物已丢失或不可访问") from error
    if len(normalized_path.parts) < 3 or normalized_path.parts[1] != "archives":
        raise ToolError("归档产物索引不合法")
    if not target_path.is_file():
        raise ToolError("归档产物已丢失或不可访问")
    return target_path, filename


@register_mcp_tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def read_zhihu_archive_artifact(
    task_id: str,
    artifact: ArchiveTextArtifact,
    offset: int = 0,
) -> ArchiveArtifactResult:
    """分页读取已完成归档任务的 JSON、Markdown 或 HTML 产物。

    Args:
        task_id: enqueue_zhihu_archive 返回的任务 ID。
        artifact: info、markdown 或 html。
        offset: 长文本分页读取时的起始字符位置。
    """
    if offset < 0:
        raise ToolError("offset 不能小于 0")
    services = require_services()
    target_path, filename = await resolve_archive_artifact(
        services,
        task_id,
        artifact,
    )
    preview_type = get_preview_type(target_path)
    expected_types = {
        "info": ResultPreviewType.JSON,
        "markdown": ResultPreviewType.MARKDOWN,
        "html": ResultPreviewType.HTML,
    }
    if preview_type != expected_types[artifact]:
        raise ToolError("归档产物类型与索引不匹配")
    try:
        if target_path.stat().st_size > MAX_TEXT_PREVIEW_SIZE:
            raise ToolError("归档文本产物过大，无法通过 MCP 读取")
        async with aiofiles.open(target_path, "r", encoding="utf-8") as file_obj:
            content = await file_obj.read()
    except ToolError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ToolError("归档产物已丢失或无法读取") from error
    if offset > len(content):
        raise ToolError("offset 超出归档文本长度")
    config = await services.mcp_config.get_config()
    end = min(offset + config["max_content_chars"], len(content))
    next_offset = end if end < len(content) else None
    return ArchiveArtifactResult(
        task_id=task_id,
        artifact=artifact,
        filename=filename,
        content_format={
            "info": "json",
            "markdown": "markdown",
            "html": "html",
        }[artifact],
        content=content[offset:end],
        offset=offset,
        total_chars=len(content),
        truncated=next_offset is not None,
        next_offset=next_offset,
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
async def get_zhihu_archive_screenshot(task_id: str) -> Image:
    """读取已完成归档任务的截图并直接返回 MCP Image。

    Args:
        task_id: enqueue_zhihu_archive 返回的任务 ID。
    """
    target_path, _filename = await resolve_archive_artifact(
        require_services(),
        task_id,
        "screenshot",
    )
    image_format = {
        ".jpeg": "jpeg",
        ".jpg": "jpeg",
        ".png": "png",
    }.get(target_path.suffix.lower())
    if get_preview_type(target_path) != ResultPreviewType.IMAGE or not image_format:
        raise ToolError("归档截图类型与索引不匹配")
    try:
        async with aiofiles.open(target_path, "rb") as file_obj:
            data = await file_obj.read(MAX_ARCHIVE_IMAGE_BYTES + 1)
    except OSError as error:
        raise ToolError("归档截图已丢失或无法读取") from error
    if len(data) > MAX_ARCHIVE_IMAGE_BYTES:
        raise ToolError("归档截图过大，无法通过 MCP 返回")
    return Image(data=data, format=image_format)


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


def is_loopback_ip(value: str) -> bool:
    """判断字符串是否为 IPv4、IPv6 或映射形式的回环地址。

    Args:
        value: 待检查的 IP 地址字符串。
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def is_loopback_authority(value: str) -> bool:
    """判断 HTTP Host 或 Origin authority 是否指向本机回环地址。

    Args:
        value: Host header 或带端口的 authority。
    """
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname or ""
        _port = parsed.port
    except ValueError:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return hostname.lower() == "localhost" or is_loopback_ip(hostname)


def is_loopback_origin(value: str) -> bool:
    """判断 Origin 是否为格式严格的本机 HTTP(S) Origin。

    Args:
        value: Origin 请求头内容。
    """
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    hostname = parsed.hostname or ""
    return hostname.lower() == "localhost" or is_loopback_ip(hostname)


def is_direct_loopback_request(scope: Scope) -> bool:
    """判断请求是否直接来自本机且未经过声明身份的代理。

    同机代理若不保留来源头且把 Host 改为回环地址，在 ASGI 层与直接本机请求无法区分。

    Args:
        scope: 当前 HTTP ASGI scope。
    """
    client = scope.get("client")
    if not client or not is_loopback_ip(str(client[0])):
        return False
    headers = {
        bytes(name).lower(): bytes(value).decode("latin-1")
        for name, value in scope.get("headers", [])
    }
    if PROXY_IDENTITY_HEADERS.intersection(headers):
        return False
    host = headers.get(b"host")
    if not host or not is_loopback_authority(host):
        return False
    origin = headers.get(b"origin")
    return origin is None or is_loopback_origin(origin)


class MCPBearerAuthMiddleware:
    """使用 Bearer Token 或可选的直接本机匿名规则保护 MCP。"""

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
        if config["allow_anonymous_local"] and is_direct_loopback_request(scope):
            await self.app(scope, receive, send)
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
            "使用已由 ZhiArchive 主服务托管的知乎登录态读取正文、问题、热榜和个人"
            "内容列表、提交归档任务、读取已完成归档产物，并按需发起二维码登录。"
            "正文读取支持知乎回答和专栏文章；问题读取返回问题描述、话题和统计；"
            "热榜最多返回三十个问题；个人列表支持回答、文章、想法、收藏夹及收藏夹"
            "内容。归档产物通过任务 ID 和受限类型读取，不使用服务器文件系统路径。"
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
