from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from archive.api.endpoints import auth, logs, zhi
from archive.api.render import templates
from archive.config import api_settings
from archive.services import AppServices, set_current_services


def configure_cors(application: FastAPI, allowed_origins: list[str]) -> None:
    """
    为 API 配置明确的跨域访问白名单。

    Args:
        application: 当前 FastAPI 应用。
        allowed_origins: 允许携带凭据访问 API 的 Origin 列表。
    """
    if not allowed_origins:
        return
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """启动和关闭单进程后台服务。"""
    services = AppServices()
    await services.start()
    application.state.services = services
    set_current_services(services)
    try:
        yield
    finally:
        set_current_services(None)
        await services.stop()


app = FastAPI(title="Zhi Archive", lifespan=lifespan)
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(logs.router, prefix="/log", tags=["log"])
app.include_router(zhi.router, prefix="/zhi", tags=["zhi"])

configure_cors(app, api_settings.cors_origins)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, bool]:
    """返回 API 与后台任务的健康状态。"""
    services: AppServices = request.app.state.services
    if not services.healthy() or not await services.store.ping():
        raise HTTPException(status_code=503, detail="background worker failed")
    return {"ok": services.healthy()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "config.html",
        context={
            "zhi_login_url": str(request.url_for("zhi:login_view")),
            "results_url": str(request.url_for("zhi:results_view")),
        },
    )
