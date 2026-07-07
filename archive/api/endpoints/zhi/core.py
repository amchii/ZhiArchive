import json
import os
import pathlib
from enum import Enum
from typing import Any

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from archive.api.render import templates
from archive.api.security import verify_user_from_cookie
from archive.core.api_client import get_api_client
from archive.core.base import BaseWorker, ConfigFilter

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


@router.get("/state_path", response_model=StatePath)
async def get_state_path():
    client = get_api_client()
    return {"path": str(await client.get_state_path())}


@router.put("/state_path", response_model=StatePath)
async def set_state_path(state_path: StatePath):
    client = get_api_client()
    await client.set_state_path_to_redis(state_path.path)
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
    client = get_api_client(name)
    return await client.configurator.get_configs(filter)


@router.put("/{name}/configs")
async def set_configs(name: WorkerName, configs: dict[str, Any]):
    client = get_api_client(name)
    await client.configurator.write_writeable_configs(configs)
    return await client.configurator.get_configs(ConfigFilter.WRITABLE)


@public_router.get("/config", response_class=HTMLResponse, name="zhi:config_view")
async def config_view(request: Request):
    return templates.TemplateResponse(
        request,
        "config.html",
        context={
            "zhi_login_url": str(request.url_for("zhi:login_view")),
        },
    )
