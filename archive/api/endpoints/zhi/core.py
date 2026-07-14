import json
import os
import pathlib
from datetime import datetime
from enum import Enum
from typing import Annotated, Any

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StringConstraints

from archive.api.render import templates
from archive.api.security import verify_user_from_cookie
from archive.config import settings
from archive.core.api_client import get_api_client
from archive.core.archiver import Archiver
from archive.core.base import BaseWorker, ConfigFilter, TargetType
from archive.core.monitor import Monitor
from archive.services import get_current_services
from archive.storage import SQLiteStore, WorkerBusyError, get_default_store

from .login import get_qrcode_task

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])
public_router = APIRouter()


class PauseStatus(BaseModel):
    pause: bool


class WorkerName(str, Enum):
    MONITOR = "monitor"
    ARCHIVER = "archiver"

    def __str__(self):
        return str(self.value)


class StatePath(BaseModel):
    path: str


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


@router.get("/state_path", response_model=StatePath)
async def get_state_path():
    client = get_api_client()
    return {"path": str(await client.get_state_path())}


@router.put("/state_path", response_model=StatePath)
async def set_state_path(state_path: StatePath):
    client = get_api_client()
    await client.set_state_path(state_path.path)
    return {"path": str(await client.get_state_path())}


@router.post(
    "/state_path/test",
    summary="测试state文件是否有效",
    description="会启动浏览器访问知乎进行测试",
    response_model=TestStateResult,
)
async def test_state_path(state_path: StatePath):
    if not pathlib.Path(state_path.path).exists():
        raise HTTPException(400, "State file not found")
    worker = BaseWorker()
    result = await worker.test_state(state_path.path)
    return result


@router.post("/states", summary="新建state文件", response_model=StatePath)
async def new_state(state: str):
    try:
        json.loads(state)
    except json.JSONDecodeError:
        raise HTTPException(400, "String must be json-serializable")
    task = get_qrcode_task(os.urandom(10).hex())
    async with aiofiles.open(task.state_path, "w", encoding="utf-8") as fp:
        await fp.write(state)
    return {"path": str(task.state_path)}


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
