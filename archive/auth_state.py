import asyncio
import hashlib
import json
import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypedDict

import aiofiles

from archive.config import default, settings
from archive.storage import SQLiteStore, get_default_store

MAX_AUTH_STATE_BYTES = 2 * 1024 * 1024
logger = logging.getLogger("auth_state")
_auth_state_lock = asyncio.Lock()


class AuthStateValidationError(ValueError):
    """表示上传内容不能转换为 Playwright storage state。"""


class AuthStateSource(str, Enum):
    """标识当前登录态的最近写入来源。"""

    UPLOAD = "upload"
    QRCODE = "qrcode"
    WORKER = "worker"
    LEGACY = "legacy"
    EXTERNAL = "external"


class AuthStateStatus(TypedDict):
    """描述当前托管登录态，但不暴露路径和 Cookie 内容。"""

    configured: bool
    valid: bool
    source: str | None
    updated_at: datetime | None
    cookie_count: int
    error: str | None


def get_managed_state_path() -> pathlib.Path:
    """返回应用托管的固定 Playwright storage state 路径。"""
    return settings.states_dir.joinpath(default.state_file)


def _require_string(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    """读取并校验 storage state 中的字符串字段。

    Args:
        value: 待校验的字段值。
        field: 用于错误信息的字段名。
        allow_empty: 是否允许空字符串。
    """
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AuthStateValidationError(f"{field} 必须是字符串")
    return value


def _normalize_same_site(value: Any, index: int) -> str:
    """把浏览器扩展导出的 SameSite 值转换为 Playwright 格式。"""
    if value is None:
        return "Lax"
    normalized = str(value).strip().lower().replace("_", "-")
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no-restriction": "None",
        "unspecified": "Lax",
    }
    if normalized not in mapping:
        raise AuthStateValidationError(f"cookies[{index}].sameSite 不是支持的值")
    return mapping[normalized]


def _normalize_expires(cookie: dict[str, Any], index: int) -> float:
    """把浏览器 Cookie 的过期字段转换为 Playwright 时间戳。"""
    if cookie.get("session") is True:
        return -1
    value = cookie.get("expires", cookie.get("expirationDate", -1))
    if value in (None, ""):
        return -1
    if isinstance(value, bool):
        raise AuthStateValidationError(f"cookies[{index}].expires 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise AuthStateValidationError(
            f"cookies[{index}].expires 必须是数字"
        ) from error


def _normalize_bool(value: Any, field: str) -> bool:
    """把常见 JSON 布尔表示转换为严格布尔值。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if value in (0, 1, "0", "1"):
        return bool(int(value))
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise AuthStateValidationError(f"{field} 必须是布尔值")


def _normalize_cookie(cookie: Any, index: int) -> dict[str, Any]:
    """把单条浏览器 Cookie 转换为 Playwright storage state 字段。"""
    if not isinstance(cookie, dict):
        raise AuthStateValidationError(f"cookies[{index}] 必须是对象")
    normalized: dict[str, Any] = {
        "name": _require_string(cookie.get("name"), f"cookies[{index}].name"),
        "value": _require_string(
            cookie.get("value"),
            f"cookies[{index}].value",
            allow_empty=True,
        ),
        "domain": _require_string(
            cookie.get("domain"),
            f"cookies[{index}].domain",
        ),
        "path": _require_string(
            cookie.get("path") or "/",
            f"cookies[{index}].path",
        ),
        "expires": _normalize_expires(cookie, index),
        "httpOnly": _normalize_bool(
            cookie.get("httpOnly"),
            f"cookies[{index}].httpOnly",
        ),
        "secure": _normalize_bool(
            cookie.get("secure"),
            f"cookies[{index}].secure",
        ),
        "sameSite": _normalize_same_site(cookie.get("sameSite"), index),
    }
    partition_key = cookie.get("partitionKey")
    if partition_key is not None:
        normalized["partitionKey"] = _require_string(
            partition_key,
            f"cookies[{index}].partitionKey",
        )
    return normalized


def _normalize_origin(origin: Any, index: int) -> dict[str, Any]:
    """校验并保留 Playwright storage state 的 localStorage 数据。"""
    if not isinstance(origin, dict):
        raise AuthStateValidationError(f"origins[{index}] 必须是对象")
    local_storage = origin.get("localStorage", [])
    if not isinstance(local_storage, list):
        raise AuthStateValidationError(f"origins[{index}].localStorage 必须是数组")
    normalized_items: list[dict[str, str]] = []
    for item_index, item in enumerate(local_storage):
        if not isinstance(item, dict):
            raise AuthStateValidationError(
                f"origins[{index}].localStorage[{item_index}] 必须是对象"
            )
        normalized_items.append(
            {
                "name": _require_string(
                    item.get("name"),
                    f"origins[{index}].localStorage[{item_index}].name",
                ),
                "value": _require_string(
                    item.get("value"),
                    f"origins[{index}].localStorage[{item_index}].value",
                    allow_empty=True,
                ),
            }
        )
    return {
        "origin": _require_string(origin.get("origin"), f"origins[{index}].origin"),
        "localStorage": normalized_items,
    }


def normalize_auth_state(payload: Any) -> dict[str, Any]:
    """将 Playwright state 或浏览器 Cookies 数组归一化。

    Args:
        payload: Playwright storage state 对象或浏览器导出的 Cookie 数组。
    """
    if isinstance(payload, list):
        cookies = payload
        origins: list[Any] = []
    elif isinstance(payload, dict):
        cookies = payload.get("cookies")
        origins = payload.get("origins", [])
    else:
        raise AuthStateValidationError("State 文件顶层必须是对象或数组")
    if not isinstance(cookies, list):
        raise AuthStateValidationError("cookies 必须是数组")
    if not cookies:
        raise AuthStateValidationError("cookies 不能为空")
    if not isinstance(origins, list):
        raise AuthStateValidationError("origins 必须是数组")
    return {
        "cookies": [
            _normalize_cookie(cookie, index) for index, cookie in enumerate(cookies)
        ],
        "origins": [
            _normalize_origin(origin, index) for index, origin in enumerate(origins)
        ],
    }


def _encode_auth_state(payload: dict[str, Any]) -> bytes:
    """将归一化后的 storage state 编码为 UTF-8 JSON。"""
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    if len(encoded) > MAX_AUTH_STATE_BYTES:
        raise AuthStateValidationError("State 文件不能超过 2 MiB")
    return encoded


class AuthStateManager:
    """管理固定位置的 Playwright 登录态文件及其元数据。"""

    def __init__(
        self,
        store: SQLiteStore | None = None,
        path: pathlib.Path | str | None = None,
    ) -> None:
        """创建登录态管理器。

        Args:
            store: 保存来源元数据的 SQLite store。
            path: 测试或内部调用使用的托管路径覆盖值。
        """
        self.store = store or get_default_store()
        self.path = pathlib.Path(path or get_managed_state_path())

    async def revision(self) -> str | None:
        """返回当前托管 state 内容的 SHA-256 修订值。"""
        try:
            async with aiofiles.open(self.path, "rb") as file_obj:
                content = await file_obj.read(MAX_AUTH_STATE_BYTES + 1)
        except FileNotFoundError:
            return None
        return hashlib.sha256(content).hexdigest()

    async def status(self) -> AuthStateStatus:
        """返回不包含服务器路径和 Cookie 内容的登录态摘要。"""
        try:
            stat = self.path.stat()
            async with aiofiles.open(self.path, "rb") as file_obj:
                content = await file_obj.read(MAX_AUTH_STATE_BYTES + 1)
        except FileNotFoundError:
            return {
                "configured": False,
                "valid": False,
                "source": None,
                "updated_at": None,
                "cookie_count": 0,
                "error": None,
            }
        updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if len(content) > MAX_AUTH_STATE_BYTES:
            return {
                "configured": True,
                "valid": False,
                "source": AuthStateSource.EXTERNAL.value,
                "updated_at": updated_at,
                "cookie_count": 0,
                "error": "State 文件超过 2 MiB",
            }
        revision = hashlib.sha256(content).hexdigest()
        metadata = await self.store.get_settings("auth_state")
        source = metadata.get("source")
        if metadata.get("revision") != revision:
            source = AuthStateSource.EXTERNAL.value
        try:
            payload = json.loads(content.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise AuthStateValidationError(
                    "Cookies 数组需要通过配置页上传后才能启用"
                )
            normalized = normalize_auth_state(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            AuthStateValidationError,
        ) as error:
            return {
                "configured": True,
                "valid": False,
                "source": str(source or AuthStateSource.EXTERNAL.value),
                "updated_at": updated_at,
                "cookie_count": 0,
                "error": str(error),
            }
        return {
            "configured": True,
            "valid": True,
            "source": str(source or AuthStateSource.EXTERNAL.value),
            "updated_at": updated_at,
            "cookie_count": len(normalized["cookies"]),
            "error": None,
        }

    async def activate(
        self,
        payload: Any,
        source: AuthStateSource | str,
    ) -> AuthStateStatus:
        """校验并原子替换当前托管登录态。

        Args:
            payload: Playwright state 对象或浏览器 Cookie 数组。
            source: 本次替换的来源标识。
        """
        normalized = normalize_auth_state(payload)
        content = _encode_auth_state(normalized)
        async with _auth_state_lock:
            await self._write_unlocked(content, source)
        return await self.status()

    async def activate_if_unchanged(
        self,
        payload: Any,
        source: AuthStateSource | str,
        expected_revision: str | None,
    ) -> bool:
        """仅当 state 未被其他操作替换时原子回写登录态。

        Args:
            payload: 浏览器上下文产生的最新 state。
            source: 本次替换的来源标识。
            expected_revision: 浏览器上下文创建时读取到的修订值。
        """
        normalized = normalize_auth_state(payload)
        content = _encode_auth_state(normalized)
        async with _auth_state_lock:
            if await self.revision() != expected_revision:
                return False
            await self._write_unlocked(content, source)
        return True

    async def _write_unlocked(
        self,
        content: bytes,
        source: AuthStateSource | str,
    ) -> None:
        """在调用方持有登录态锁时写入临时文件并替换正式文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            async with aiofiles.open(temp_path, "xb") as file_obj:
                await file_obj.write(content)
                await file_obj.flush()
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)
        await self.store.set_settings(
            "auth_state",
            {
                "source": str(
                    source.value if isinstance(source, AuthStateSource) else source
                ),
                "revision": hashlib.sha256(content).hexdigest(),
            },
        )

    async def migrate_legacy_path(self) -> bool:
        """迁移旧 SQLite 绝对路径配置并删除该运行时配置。"""
        global_settings = await self.store.get_settings("global")
        legacy_value = global_settings.get("state_path")
        if legacy_value is None:
            return False
        migrated = False
        legacy_path = pathlib.Path(str(legacy_value))
        if legacy_path.exists():
            try:
                async with aiofiles.open(legacy_path, "rb") as file_obj:
                    content = await file_obj.read(MAX_AUTH_STATE_BYTES + 1)
                if len(content) > MAX_AUTH_STATE_BYTES:
                    raise AuthStateValidationError("旧 State 文件超过 2 MiB")
                payload = json.loads(content.decode("utf-8-sig"))
                await self.activate(payload, AuthStateSource.LEGACY)
                migrated = True
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                AuthStateValidationError,
            ) as error:
                logger.warning(
                    "无法迁移旧 storage state 路径 %s: %s", legacy_path, error
                )
        await self.store.delete_settings("global", ["state_path"])
        return migrated
