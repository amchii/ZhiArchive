from typing import Annotated

import jwt
from fastapi import Cookie, HTTPException

from archive.config import api_settings
from archive.utils.encrypt import decode_jwt


def verify_user_from_cookie(token: Annotated[str | None, Cookie()] = None) -> str:
    if not token:
        raise HTTPException(status_code=401)
    try:
        username = decode_jwt(token)["username"]
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(status_code=401)

    if username != api_settings.username:
        raise HTTPException(status_code=401)
    return username
