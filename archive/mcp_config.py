import hashlib
import secrets
from typing import TypedDict

from archive.storage import SQLiteStore

MCP_SETTINGS_PREFIX = "mcp"
DEFAULT_READER_TIMEOUT_SECONDS = 60
DEFAULT_MAX_CONTENT_CHARS = 50_000


class MCPRuntimeConfig(TypedDict):
    """表示可由主服务动态管理的 MCP 配置。"""

    enabled: bool
    allow_anonymous_local: bool
    token_configured: bool
    reader_timeout_seconds: int
    max_content_chars: int


def hash_mcp_token(token: str) -> str:
    """计算 MCP Bearer Token 的 SHA-256 摘要。

    Args:
        token: 待保存或验证的高熵随机 Token。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MCPConfigManager:
    """通过主服务 SQLite 配置控制 MCP 开关和访问 Token。"""

    def __init__(self, store: SQLiteStore) -> None:
        """创建 MCP 配置管理器。

        Args:
            store: 主服务持有的 SQLite store。
        """
        self.store = store

    async def get_config(self) -> MCPRuntimeConfig:
        """读取不包含 Token 摘要的 MCP 运行配置。"""
        stored = await self.store.get_settings(MCP_SETTINGS_PREFIX)
        return MCPRuntimeConfig(
            enabled=bool(stored.get("enabled", True)),
            allow_anonymous_local=bool(stored.get("allow_anonymous_local", True)),
            token_configured=bool(stored.get("token_hash")),
            reader_timeout_seconds=int(
                stored.get(
                    "reader_timeout_seconds",
                    DEFAULT_READER_TIMEOUT_SECONDS,
                )
            ),
            max_content_chars=int(
                stored.get("max_content_chars", DEFAULT_MAX_CONTENT_CHARS)
            ),
        )

    async def update_config(
        self,
        *,
        enabled: bool,
        allow_anonymous_local: bool | None,
        reader_timeout_seconds: int,
        max_content_chars: int,
    ) -> MCPRuntimeConfig:
        """更新 MCP 开关及 Reader 运行参数。

        Args:
            enabled: 是否允许 MCP 请求进入工具层。
            allow_anonymous_local: 是否允许直接本机匿名请求；为空时保留现有值。
            reader_timeout_seconds: 单次即时读取的最大等待秒数。
            max_content_chars: MCP 单次返回正文的最大字符数。
        """
        values = {
            "enabled": enabled,
            "reader_timeout_seconds": reader_timeout_seconds,
            "max_content_chars": max_content_chars,
        }
        if allow_anonymous_local is not None:
            values["allow_anonymous_local"] = allow_anonymous_local
        await self.store.set_settings(MCP_SETTINGS_PREFIX, values)
        return await self.get_config()

    async def rotate_token(self) -> tuple[str, MCPRuntimeConfig]:
        """生成新 MCP Token、保存摘要并返回一次明文。"""
        token = secrets.token_urlsafe(32)
        await self.store.set_settings(
            MCP_SETTINGS_PREFIX,
            {"token_hash": hash_mcp_token(token)},
        )
        return token, await self.get_config()

    async def verify_token(self, token: str) -> bool:
        """使用恒定时间比较验证 MCP Bearer Token。

        Args:
            token: MCP 客户端提交的 Bearer Token。
        """
        if not token:
            return False
        stored = await self.store.get_settings(MCP_SETTINGS_PREFIX)
        token_hash = str(stored.get("token_hash") or "")
        if not token_hash:
            return False
        return secrets.compare_digest(hash_mcp_token(token), token_hash)
