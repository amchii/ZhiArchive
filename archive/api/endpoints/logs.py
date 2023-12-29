import logging
from enum import Enum

import aiofiles
import aiofiles.os
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from archive.api.security import verify_user_from_cookie

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])


class LoggerName(str, Enum):
    default = "default"
    login_worker = "login_worker"
    monitor = "monitor"
    archiver = "archiver"


@router.get("/{name}/logs", response_class=PlainTextResponse)
async def logs(name: LoggerName, size_kb: int = 1000):
    logger = logging.getLogger(name.value)
    file_handler = None
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            file_handler = handler
            break
    if not file_handler:
        return PlainTextResponse("File handler not found", status_code=404)

    filename = file_handler.baseFilename
    try:
        stat = await aiofiles.os.stat(filename)
    except FileNotFoundError:
        return PlainTextResponse("")
    if stat.st_size < size_kb * 1024:
        seek = 0
    else:
        seek = stat.st_size - size_kb * 1024

    async with aiofiles.open(filename, "r", encoding="utf-8") as fp:
        await fp.seek(seek)
        data = await fp.read()
    return PlainTextResponse(data)
