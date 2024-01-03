import uuid
from datetime import datetime

from pathvalidate import sanitize_filename


def dt_str(dt: datetime = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%Y%m%d%H%M%S")


def dt_fromisoformat(dt: str) -> datetime:
    if isinstance(dt, datetime):
        return dt
    return datetime.fromisoformat(dt)


def dt_toisoformat(dt: datetime) -> str:
    return dt.isoformat()


def get_validate_filename(filename: str) -> str:
    return sanitize_filename(filename, replacement_text="_")


def uuid_hex() -> str:
    return uuid.uuid4().hex
