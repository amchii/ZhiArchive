import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from archive.api.render import templates
from archive.api.security import verify_user_from_cookie
from archive.config import settings
from archive.core.login import QRCodeTask, QRCodeTaskStatus, ZhiLoginClient
from archive.services import get_current_services, new_qrcode_task

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])
public_router = APIRouter()


def get_qrcode_task(prefix: str) -> QRCodeTask:
    return QRCodeTask(
        settings.states_dir.joinpath(f"{prefix}.qrcode.png"),
        settings.states_dir.joinpath(f"{prefix}.state.json"),
    )


def get_task_prefix(task: QRCodeTask) -> str:
    return task.qrcode_path.name.split(".")[0]


class QRCodeTaskResponse(BaseModel):
    qrcode: str


class QRCodeScanStatusResponse(BaseModel):
    status: QRCodeTaskStatus


class QRCodeInfo(BaseModel):
    qrcode_path: str
    state_path: str


@public_router.get("", response_class=HTMLResponse, name="zhi:login_view")
async def login_view(request: Request):
    return templates.TemplateResponse(
        request,
        "qrcode.html",
        context={
            "redirect_url": str(request.url_for("index")),
        },
    )


@router.get("/qrcode/{prefix}/info", response_model=QRCodeInfo)
async def qrcode_info(prefix: str):
    task = get_qrcode_task(prefix)
    return {"qrcode_path": str(task.qrcode_path), "state_path": str(task.state_path)}


@router.get("/qrcode/new", response_model=QRCodeTaskResponse)
async def new_login_qrcode():
    qrcode_task = new_qrcode_task()
    services = get_current_services()
    if services is not None:
        task = await services.start_login_task(qrcode_task)
    else:
        client = ZhiLoginClient()
        task = await client.new_task(qrcode_task)
    return {"qrcode": get_task_prefix(task)}


@router.get("/qrcode/{prefix}", response_class=FileResponse)
async def login_qrcode(prefix: str, timeout: int = 30):
    start = time.perf_counter()
    qrcode_task = get_qrcode_task(prefix)
    qrcode_path = qrcode_task.qrcode_path
    while start + timeout > time.perf_counter():
        if qrcode_path.exists():
            return FileResponse(qrcode_path)
        await asyncio.sleep(0.2)
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
    client = ZhiLoginClient()
    await client.store.set_state_path(qrcode_task.state_path)
    return str(
        await client.store.get_state_path(settings.states_dir / "zhihu.state.json")
    )
