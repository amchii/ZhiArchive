from unittest.mock import AsyncMock, MagicMock

import pytest

from archive.auth_state import AuthStateSource
from archive.core.login import QRCodeTask, QRCodeTaskStatus, ZhiLogin


def make_playwright_context() -> tuple[MagicMock, MagicMock, MagicMock]:
    """构造二维码登录流程所需的 Playwright mock。"""
    playwright = MagicMock()
    browser = MagicMock()
    context = MagicMock()
    page = MagicMock()
    page.goto = AsyncMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock(return_value=None)
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock(
        return_value={
            "cookies": [
                {
                    "name": "z_c0",
                    "value": "value",
                    "domain": ".zhihu.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }
    )
    return playwright, browser, context


@pytest.mark.asyncio
async def test_qrcode_login_activates_state_before_ok(tmp_path) -> None:
    """验证二维码任务只在托管 state 激活成功后标记为完成。"""
    playwright, _browser, context = make_playwright_context()
    events: list[str] = []

    async def activate_state(*_args: object) -> None:
        """记录托管 state 激活时序。"""
        events.append("activate")

    async def set_status(_task_name: str, status: QRCodeTaskStatus) -> None:
        """记录二维码任务状态更新时序。"""
        events.append(status.value)

    auth_state = MagicMock()
    auth_state.activate = AsyncMock(side_effect=activate_state)
    login = ZhiLogin(auth_state=auth_state)
    login.set_qrcode_task_status = AsyncMock(side_effect=set_status)
    login._wait_qrcode = AsyncMock(side_effect=[None, b"qrcode"])
    login._wait_for_login_success = AsyncMock(return_value=True)
    task = QRCodeTask(tmp_path / "login.qrcode.png")

    result = await login.get_qrcode(playwright, task)

    assert result == b"qrcode"
    auth_state.activate.assert_awaited_once_with(
        context.storage_state.return_value,
        AuthStateSource.QRCODE,
    )
    login.set_qrcode_task_status.assert_awaited_with(
        task.task_name,
        QRCodeTaskStatus.OK,
    )
    assert events.index("activate") < events.index(QRCodeTaskStatus.OK.value)


@pytest.mark.asyncio
async def test_qrcode_timeout_does_not_replace_state(tmp_path) -> None:
    """验证二维码登录超时后不会保存未登录的浏览器 state。"""
    playwright, _browser, context = make_playwright_context()
    auth_state = MagicMock()
    auth_state.activate = AsyncMock()
    login = ZhiLogin(auth_state=auth_state)
    login.set_qrcode_task_status = AsyncMock()
    login._wait_qrcode = AsyncMock(side_effect=[None, b"qrcode"])
    login._wait_for_login_success = AsyncMock(return_value=False)
    task = QRCodeTask(tmp_path / "login.qrcode.png")

    result = await login.get_qrcode(playwright, task)

    assert result == b"qrcode"
    auth_state.activate.assert_not_awaited()
    context.storage_state.assert_not_awaited()
