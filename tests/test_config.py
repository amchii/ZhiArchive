import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from archive.api.app import configure_cors
from archive.config import APISettings


def test_api_settings_parse_cors_origin_allowlist() -> None:
    """验证 API 配置可以解析、标准化并去重跨域来源白名单。"""
    settings = APISettings(
        _env_file=None,
        cors_allowed_origins=(
            "HTTPS://Console.Example.COM/, http://127.0.0.1:3000, "
            "https://console.example.com"
        ),
    )

    assert settings.cors_origins == [
        "https://console.example.com",
        "http://127.0.0.1:3000",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "ftp://console.example.com",
        "https://console.example.com/path",
        "https://user:password@console.example.com",
    ],
)
def test_api_settings_reject_invalid_cors_origins(origin: str) -> None:
    """
    验证通配符、非 HTTP 协议、路径和内嵌凭据不能进入 CORS 白名单。

    Args:
        origin: 待校验的跨域来源配置。
    """
    settings = APISettings(_env_file=None, cors_allowed_origins=origin)

    with pytest.raises(ValueError):
        _ = settings.cors_origins


def test_configure_cors_only_adds_middleware_for_allowlist() -> None:
    """验证空白名单保持同源模式，明确白名单才启用 CORS 中间件。"""
    same_origin_app = FastAPI()
    configure_cors(same_origin_app, [])
    assert same_origin_app.user_middleware == []

    cors_app = FastAPI()
    configure_cors(cors_app, ["https://console.example.com"])

    assert len(cors_app.user_middleware) == 1
    middleware = cors_app.user_middleware[0]
    assert middleware.cls is CORSMiddleware
    assert middleware.kwargs["allow_origins"] == ["https://console.example.com"]
    assert middleware.kwargs["allow_credentials"] is True
