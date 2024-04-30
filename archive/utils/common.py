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


def get_validate_filename(filename: str, safe_cn_length=50) -> str:
    """
    知乎的文章标题最多100个汉字
    sanitize_filename方法不能正确处理汉字（255个汉字依然超出默认长度：255bytes）
    """
    filename = sanitize_filename(filename, replacement_text="_")
    try:
        validate_filename(filename)
    except ValidationError as e:
        if e.reason == ErrorReason.INVALID_LENGTH:
            return filename[:safe_cn_length]
    return filename


def uuid_hex() -> str:
    return uuid.uuid4().hex
