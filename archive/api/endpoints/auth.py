from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import HTMLResponse

from archive.api.render import templates
from archive.config import api_settings
from archive.utils.encrypt import encode_jwt

router = APIRouter()


@router.post("/login")
def login(username: str = Body(), password: str = Body()):
    if username == api_settings.username and password == api_settings.password:
        response = Response()
        token = encode_jwt({"username": username})
        response.set_cookie("token", token, max_age=api_settings.cookies_max_age)
        return response
    return Response(status_code=401, content=b"Invalid username or password")


@router.get("/login", response_class=HTMLResponse)
def login_view(request: Request):
    return templates.TemplateResponse(
        "login.html",
        context={"request": request, "login_url": request.url_for("login")},
    )
