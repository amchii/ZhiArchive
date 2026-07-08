import uuid
from datetime import datetime

from pathvalidate import (
    ErrorReason,
    ValidationError,
    sanitize_filename,
    validate_filename,
)


def dt_str(dt: datetime = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%Y%m%d%H%M%S")


def dt_fromisoformat(dt: str) -> datetime:
    if isinstance(dt, datetime):
        return dt
    return datetime.fromisoformat(dt)


def dt_toisoformat(dt: datetime) -> str:
    return dt.isoformat()


def get_validate_filename(
    filename: str,
    safe_cn_length: int = 50,
    reserved_suffix: str = "",
) -> str:
    """
    清理文件名，并在名称过长时保留指定后缀。

    Args:
        filename: 待处理的文件名。
        safe_cn_length: 名称过长时保留的前缀字符数。
        reserved_suffix: 截断时必须保留的短 ID、扩展名等后缀。
    """
    filename = sanitize_filename(filename, replacement_text="_", max_len=None)
    try:
        validate_filename(filename)
    except ValidationError as e:
        if e.reason == ErrorReason.INVALID_LENGTH:
            if reserved_suffix and filename.endswith(reserved_suffix):
                filename = filename[: -len(reserved_suffix)]
            return f"{filename[:safe_cn_length]}{reserved_suffix}"
    return filename


def uuid_hex() -> str:
    return uuid.uuid4().hex
