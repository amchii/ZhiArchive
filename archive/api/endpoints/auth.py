from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from archive.api.render import templates
from archive.api.security import verify_user_from_cookie
from archive.config import api_settings
from archive.utils.encrypt import encode_jwt

router = APIRouter()


@router.post("/login")
def login(username: Annotated[str, Body()], password: Annotated[str, Body()]):
    if username == api_settings.username and password == api_settings.password:
        response = Response()
        token = encode_jwt({"username": username})
        response.set_cookie("token", token, max_age=api_settings.cookies_max_age)
        return response
    return Response(status_code=401, content=b"Invalid username or password")


@router.get("/login", response_class=HTMLResponse)
def login_view(request: Request):
    redirect_url = request.query_params.get(
        "redirect", str(request.url_for("zhi:config_view"))
    )
    return templates.TemplateResponse(
        request,
        "login.html",
        context={
            "login_url": str(request.url_for("login")),
            "redirect_url": redirect_url,
        },
    )


class Me(BaseModel):
    username: str


@router.get("/me", response_model=Me)
def me(request: Request, username: Annotated[str, Depends(verify_user_from_cookie)]):
    return {"username": username}
