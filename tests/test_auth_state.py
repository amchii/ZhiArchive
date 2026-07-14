import json

import pytest

from archive.auth_state import (
    AuthStateManager,
    AuthStateSource,
    AuthStateValidationError,
    normalize_auth_state,
)
from archive.storage import SQLiteStore


def make_cookie(value: str = "cookie-value") -> dict[str, object]:
    """构造一条浏览器扩展风格的知乎 Cookie。"""
    return {
        "name": "z_c0",
        "value": value,
        "domain": ".zhihu.com",
        "path": "/",
        "expirationDate": 1_900_000_000,
        "httpOnly": True,
        "secure": True,
        "sameSite": "no_restriction",
    }


def test_normalize_browser_cookie_array() -> None:
    """验证浏览器扩展导出的 Cookie 数组可转换为 Playwright state。"""
    normalized = normalize_auth_state([make_cookie()])

    cookie = normalized["cookies"][0]
    assert cookie["name"] == "z_c0"
    assert cookie["expires"] == 1_900_000_000
    assert cookie["sameSite"] == "None"
    assert normalized["origins"] == []


def test_normalize_auth_state_rejects_empty_cookies() -> None:
    """验证不包含 Cookie 的文件不能覆盖当前登录态。"""
    with pytest.raises(AuthStateValidationError, match="cookies 不能为空"):
        normalize_auth_state({"cookies": [], "origins": []})


@pytest.mark.asyncio
async def test_activate_auth_state_writes_managed_file(tmp_path) -> None:
    """验证上传内容原子写入托管位置且状态不暴露敏感内容。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    manager = AuthStateManager(store, tmp_path / "states/zhihu.state.json")

    status = await manager.activate([make_cookie()], AuthStateSource.UPLOAD)
    saved = json.loads(manager.path.read_text(encoding="utf-8"))

    assert saved["cookies"][0]["value"] == "cookie-value"
    assert status["configured"] is True
    assert status["valid"] is True
    assert status["source"] == "upload"
    assert status["cookie_count"] == 1
    assert "path" not in status
    assert "cookies" not in status
    await store.close()


@pytest.mark.asyncio
async def test_worker_does_not_overwrite_newer_uploaded_state(tmp_path) -> None:
    """验证旧浏览器上下文不能覆盖用户刚上传的新登录态。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    manager = AuthStateManager(store, tmp_path / "states/zhihu.state.json")
    await manager.activate([make_cookie("old")], AuthStateSource.UPLOAD)
    old_revision = await manager.revision()
    await manager.activate([make_cookie("new")], AuthStateSource.UPLOAD)

    updated = await manager.activate_if_unchanged(
        [make_cookie("stale-worker")],
        AuthStateSource.WORKER,
        old_revision,
    )
    saved = json.loads(manager.path.read_text(encoding="utf-8"))

    assert updated is False
    assert saved["cookies"][0]["value"] == "new"
    await store.close()


@pytest.mark.asyncio
async def test_migrate_legacy_state_path(tmp_path) -> None:
    """验证旧绝对路径中的有效 state 会迁移到应用托管位置。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    await store.seed_defaults()
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps([make_cookie()]), encoding="utf-8")
    await store.set_settings("global", {"state_path": str(legacy_path)})
    manager = AuthStateManager(store, tmp_path / "states/zhihu.state.json")

    migrated = await manager.migrate_legacy_path()

    assert migrated is True
    assert manager.path.exists()
    assert "state_path" not in await store.get_settings("global")
    assert (await manager.status())["source"] == "legacy"
    await store.close()


@pytest.mark.asyncio
async def test_legacy_selected_state_overrides_existing_default(tmp_path) -> None:
    """验证旧配置选择的自定义 state 在迁移时优先于默认文件。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    active_path = tmp_path / "states/zhihu.state.json"
    manager = AuthStateManager(store, active_path)
    await manager.activate([make_cookie("default")], AuthStateSource.UPLOAD)
    legacy_path = tmp_path / "selected.json"
    legacy_path.write_text(json.dumps([make_cookie("selected")]), encoding="utf-8")
    await store.set_settings("global", {"state_path": str(legacy_path)})

    migrated = await manager.migrate_legacy_path()
    saved = json.loads(active_path.read_text(encoding="utf-8"))

    assert migrated is True
    assert saved["cookies"][0]["value"] == "selected"
    await store.close()


@pytest.mark.asyncio
async def test_external_cookie_array_requires_import(tmp_path) -> None:
    """验证直接放入托管目录的 Cookies 数组不会被误报为可直接加载。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    state_path = tmp_path / "states/zhihu.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps([make_cookie()]), encoding="utf-8")
    manager = AuthStateManager(store, state_path)

    status = await manager.status()

    assert status["configured"] is True
    assert status["valid"] is False
    assert status["error"] is not None
    assert "配置页上传" in status["error"]
    await store.close()
