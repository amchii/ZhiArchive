import json
import os
from enum import Enum
from typing import Any

import aiofiles
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from archive.core.base import APIClient

from ...render import templates
from . import PauseStatus
from .login import get_qrcode_task

router = APIRouter()


class WorkerName(str, Enum):
    MONITOR = "monitor"
    ARCHIVER = "archiver"

    def __str__(self):
        return str(self.value)


class StatePath(BaseModel):
    path: str


@router.get("/state_path", response_model=StatePath)
async def get_state_path():
    client = APIClient()
    return {"path": str(await client.get_state_path())}


@router.put("/state_path", response_model=StatePath)
async def set_state_path(state_path: StatePath):
    client = APIClient()
    await client.set_state_path_to_redis(state_path.path)
    return {"path": str(await client.get_state_path())}


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
    client = APIClient(name)
    if status.pause:
        await client.pause()
    else:
        await client.resume()
    return {"pause": await client.need_pause()}


@router.get("/{name}/pause", response_model=PauseStatus)
async def pause_status(name: WorkerName):
    client = APIClient(name)
    return {"pause": await client.need_pause()}


@router.get("/{name}/configs")
async def get_configs(name: WorkerName) -> dict[str, Any]:
    client = APIClient(name)
    return await client.get_configs_from_redis()


@router.put("/{name}/configs")
async def set_configs(name: WorkerName, configs: dict[str, Any]):
    client = APIClient(name)
    await client.set_configs_to_redis(configs)
    return configs


@router.get("/config", response_class=HTMLResponse)
async def config_view(request: Request):
    return templates.TemplateResponse("config.html", context={"request": request})
