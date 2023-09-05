import typing

import jwt
from cryptography.fernet import Fernet

from archive.config import settings


def encode_jwt(obj: dict, key=settings.secret_key):
    return jwt.encode(obj, key=key, algorithm=settings.algorithm)


def decode_jwt(token: str, key=settings.secret_key):
    return jwt.decode(token, key=key, algorithms=[settings.algorithm])


def encrypt_data(
    data: bytes, key: typing.Union[str, bytes] = settings.secret_key
) -> bytes:
    fernet = Fernet(key)
    return fernet.encrypt(data)


def decrypt_data(
    token: bytes, key: typing.Union[str, bytes] = settings.secret_key
) -> bytes:
    fernet = Fernet(key)
    return fernet.decrypt(token)
