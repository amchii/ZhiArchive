import json
import os

import aiofiles
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from archive.api.security import verify_user_from_cookie
from archive.core import APIClient

from .zhi_login import get_qrcode_task

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])


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
