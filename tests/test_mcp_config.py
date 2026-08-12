import pytest

from archive.mcp_config import MCPConfigManager
from archive.storage import SQLiteStore


@pytest.mark.asyncio
async def test_mcp_token_is_stored_as_hash_and_rotates_immediately(tmp_path) -> None:
    """验证主服务只保存 Token 摘要，轮换后旧 Token 立即失效。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    manager = MCPConfigManager(store)

    first_token, first_config = await manager.rotate_token()
    stored = await store.get_settings("mcp")

    assert first_config["token_configured"] is True
    assert first_token not in stored.values()
    assert await manager.verify_token(first_token) is True

    second_token, _ = await manager.rotate_token()

    assert second_token != first_token
    assert await manager.verify_token(first_token) is False
    assert await manager.verify_token(second_token) is True
    await store.close()


@pytest.mark.asyncio
async def test_mcp_runtime_config_uses_safe_defaults(tmp_path) -> None:
    """验证未配置时 MCP 和严格本机匿名访问均默认开启。"""
    store = SQLiteStore(tmp_path / "zhi.sqlite3")
    await store.connect()
    manager = MCPConfigManager(store)

    config = await manager.get_config()

    assert config == {
        "enabled": True,
        "allow_anonymous_local": True,
        "token_configured": False,
        "reader_timeout_seconds": 60,
        "max_content_chars": 50_000,
    }
    await store.close()
