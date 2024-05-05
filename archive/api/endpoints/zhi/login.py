import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from archive.api.render import templates
from archive.config import settings
from archive.core.api_client import get_api_client
from archive.core.login import QRCodeTask, QRCodeTaskStatus, ZhiLoginClient

router = APIRouter()


def get_qrcode_task(prefix: str) -> QRCodeTask:
    return QRCodeTask(
        settings.states_dir.joinpath(f"{prefix}.qrcode.png"),
        settings.states_dir.joinpath(f"{prefix}.state.json"),
    )


class QRCodeTaskResponse(BaseModel):
    qrcode: str


class QRCodeScanStatusResponse(BaseModel):
    status: QRCodeTaskStatus


class QRCodeInfo(BaseModel):
    qrcode_path: str
    state_path: str


@router.get("", response_class=HTMLResponse)
async def login_view(request: Request):
    return templates.TemplateResponse("qrcode.html", context={"request": request})


@router.get("/qrcode/{prefix}/info", response_model=QRCodeInfo)
async def qrcode_info(prefix: str):
    task = get_qrcode_task(prefix)
    return {"qrcode_path": task.qrcode_path, "state_path": task.state_path}


@router.get("/qrcode/new", response_model=QRCodeTaskResponse)
async def new_login_qrcode():
    prefix = os.urandom(10).hex()
    qrcode_task = get_qrcode_task(prefix)
    client = ZhiLoginClient()
    await client.new_task(qrcode_task)
    return {"qrcode": prefix}


@router.get("/qrcode/{prefix}", response_class=FileResponse)
async def login_qrcode(prefix: str, timeout: int = 10):
    start = time.perf_counter()
    qrcode_task = get_qrcode_task(prefix)
    qrcode_path = qrcode_task.qrcode_path
    while start + timeout > time.perf_counter():
        if qrcode_path.exists():
            return FileResponse(qrcode_path)
    raise HTTPException(status_code=404)


@router.get("/qrcode/{prefix}/scan_status", response_model=QRCodeScanStatusResponse)
async def qrcode_scan_status(prefix: str):
    qrcode_task = get_qrcode_task(prefix)
    client = ZhiLoginClient()
    status = await client.get_qrcode_task_status(qrcode_task.task_name)
    return {"status": status}


@router.get("/state/{prefix}", response_class=FileResponse)
async def login_state(prefix: str):
    qrcode_task = get_qrcode_task(prefix)
    if not qrcode_task.state_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(qrcode_task.state_path)


@router.post("/state/{prefix}/use")
async def use_state(prefix: str) -> str:
    qrcode_task = get_qrcode_task(prefix)
    client = get_api_client()
    await client.set_state_path_to_redis(qrcode_task.state_path)
    return str(await client.get_state_path())
