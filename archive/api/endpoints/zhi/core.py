import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StringConstraints

from archive.api.render import templates
from archive.api.security import verify_user_from_cookie
from archive.auth_state import (
    MAX_AUTH_STATE_BYTES,
    AuthStateManager,
    AuthStateSource,
    AuthStateValidationError,
)
from archive.config import settings
from archive.core.api_client import get_api_client
from archive.core.archiver import Archiver
from archive.core.base import BaseWorker, ConfigFilter, TargetType
from archive.core.monitor import Monitor
from archive.services import get_current_services
from archive.storage import SQLiteStore, WorkerBusyError, get_default_store

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])
public_router = APIRouter()


class PauseStatus(BaseModel):
    pause: bool


class WorkerName(str, Enum):
    MONITOR = "monitor"
    ARCHIVER = "archiver"

    def __str__(self):
        return str(self.value)


class AuthStateResponse(BaseModel):
    configured: bool
    valid: bool
    source: str | None
    updated_at: datetime | None
    cookie_count: int
    error: str | None


class TestStateResult(BaseModel):
    test_url: str
    ok: bool


class ArchiveURLRequest(BaseModel):
    url: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
    ]


class ArchiveTaskResult(BaseModel):
    task_id: str
    url: str
    target_type: TargetType


class MonitorCheckpoint(BaseModel):
    people: str
    fetch_until: datetime
    latest_dt: datetime | None


class MonitorCheckpointUpdate(BaseModel):
    fetch_until: datetime


def get_store() -> SQLiteStore:
    """获取当前 API 进程使用的 SQLite store。"""
    services = get_current_services()
    return services.store if services is not None else get_default_store()


def get_auth_state_manager() -> AuthStateManager:
    """获取当前 API 进程使用的托管登录态管理器。"""
    services = get_current_services()
    return (
        services.auth_state if services is not None else AuthStateManager(get_store())
    )


async def read_auth_state_payload(request: Request) -> Any:
    """读取受大小限制的 JSON state 请求体。"""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_AUTH_STATE_BYTES:
                raise HTTPException(status_code=413, detail="State 文件不能超过 2 MiB")
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length 无效")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_AUTH_STATE_BYTES:
            raise HTTPException(status_code=413, detail="State 文件不能超过 2 MiB")
        chunks.append(chunk)
    if not chunks:
        raise HTTPException(status_code=400, detail="State 文件不能为空")
    try:
        return json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=422, detail="State 文件不是有效 JSON"
        ) from error


async def get_current_people(store: SQLiteStore) -> str:
    """从 SQLite 全局配置读取当前目标用户。"""
    configs = await store.get_settings("global")
    people = str(configs.get("people") or settings.people).strip()
    if not people:
        raise HTTPException(status_code=500, detail="people is not configured")
    return people


def get_worker_config_client(name: WorkerName) -> BaseWorker:
    """创建只用于配置读写的 worker 对象。"""
    store = get_store()
    if name == WorkerName.ARCHIVER:
        return Archiver(store=store)
    if name == WorkerName.MONITOR:
        return Monitor(store=store)
    worker = BaseWorker(store=store)
    worker.name = name.value
    return worker


@router.get("/auth_state", response_model=AuthStateResponse)
async def get_auth_state() -> dict[str, Any]:
    """读取当前托管登录态摘要。"""
    return await get_auth_state_manager().status()


@router.put("/auth_state", response_model=AuthStateResponse)
async def upload_auth_state(request: Request) -> dict[str, Any]:
    """上传并启用 Playwright state 或浏览器导出的 Cookies JSON。"""
    payload = await read_auth_state_payload(request)
    try:
        return await get_auth_state_manager().activate(
            payload,
            AuthStateSource.UPLOAD,
        )
    except AuthStateValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/auth_state/test",
    summary="测试当前托管 state 是否有效",
    description="会启动浏览器访问知乎进行测试",
    response_model=TestStateResult,
)
async def test_auth_state() -> dict[str, Any]:
    """启动一次交互浏览器验证当前托管登录态。"""
    manager = get_auth_state_manager()
    status = await manager.status()
    if not status["configured"]:
        raise HTTPException(status_code=400, detail="尚未上传或生成登录状态")
    if not status["valid"]:
        raise HTTPException(status_code=422, detail=status["error"])
    worker = BaseWorker(store=get_store(), init_state_path=manager.path)
    services = get_current_services()
    if services is None:
        return await worker.test_state(manager.path)
    async with services.interactive_browser_semaphore:
        return await worker.test_state(manager.path)


@router.put("/{name}/pause", response_model=PauseStatus)
async def pause(name: WorkerName, status: PauseStatus):
    client = get_api_client(name)
    if status.pause:
        await client.pause()
    else:
        await client.resume()
    return {"pause": await client.need_pause()}


@router.get("/{name}/pause", response_model=PauseStatus)
async def pause_status(name: WorkerName):
    client = get_api_client(name)
    return {"pause": await client.need_pause()}


@router.get("/configs")
async def get_global_configs() -> dict[str, Any]:
    client = get_api_client()
    return await client.global_configurator.get_configs()


@router.put("/configs")
async def set_global_configs(configs: dict[str, Any]) -> dict[str, Any]:
    if "people" in configs and not str(configs["people"]).strip():
        raise HTTPException(status_code=400, detail="people must not be empty")
    client = get_api_client()
    await client.global_configurator.write_configs(configs)
    return await client.global_configurator.get_configs()


@router.get("/{name}/configs")
async def get_configs(
    name: WorkerName, filter: ConfigFilter = ConfigFilter.ALL
) -> dict[str, Any]:
    client = get_worker_config_client(name)
    return await client.configurator.get_configs(filter)


@router.put("/{name}/configs")
async def set_configs(name: WorkerName, configs: dict[str, Any]):
    client = get_worker_config_client(name)
    await client.configurator.write_writeable_configs(configs)
    return await client.configurator.get_configs(ConfigFilter.WRITABLE)


@router.get("/monitor/checkpoint", response_model=MonitorCheckpoint)
async def get_monitor_checkpoint():
    """
    读取当前目标用户的 monitor 抓取进度。
    """
    store = get_store()
    people = await get_current_people(store)
    checkpoint = await store.get_monitor_checkpoint(people)
    return {
        "people": people,
        "fetch_until": checkpoint["fetch_until"],
        "latest_dt": checkpoint["latest_dt"],
    }


@router.put("/monitor/checkpoint", response_model=MonitorCheckpoint)
async def set_monitor_checkpoint(payload: MonitorCheckpointUpdate):
    """
    修改当前目标用户下一轮 monitor 使用的抓取截止时间。

    Args:
        payload: 新的抓取截止时间。
    """
    store = get_store()
    people = await get_current_people(store)
    try:
        updated = await store.set_monitor_checkpoint_if_idle(
            people,
            payload.fetch_until,
        )
    except WorkerBusyError:
        raise HTTPException(
            status_code=409,
            detail="请先暂停 Monitor，并等待当前抓取结束后再修改抓取进度",
        )
    return {
        "people": people,
        "fetch_until": updated["fetch_until"],
        "latest_dt": updated["latest_dt"],
    }


@router.post("/archiver/tasks", response_model=ArchiveTaskResult)
async def enqueue_archive_task(payload: ArchiveURLRequest) -> dict[str, Any]:
    """
    将知乎回答或文章链接加入 archiver 队列。

    Args:
        payload: 包含待归档链接的请求数据。
    """
    client = get_api_client(Archiver.name)
    try:
        _task, item = await client.enqueue_url(payload.url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "task_id": item["id"],
        "url": item["target"]["link"],
        "target_type": item["meta"]["target_type"],
    }


@public_router.get("/config", response_class=HTMLResponse, name="zhi:config_view")
async def config_view(request: Request):
    return templates.TemplateResponse(
        request,
        "config.html",
        context={
            "zhi_login_url": str(request.url_for("zhi:login_view")),
            "results_url": str(request.url_for("zhi:results_view")),
        },
    )
