import aiofiles
import aiofiles.os
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from archive.api.security import verify_user_from_cookie
from archive.logger import file_handler

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])


@router.get("/logs", response_class=PlainTextResponse)
async def logs(size_kb: int = 1000):
    filename = file_handler.baseFilename
    try:
        stat = await aiofiles.os.stat(filename)
    except FileNotFoundError:
        return PlainTextResponse("")
    if stat.st_size < size_kb * 1024:
        seek = 0
    else:
        seek = stat.st_size - size_kb * 1024

    async with aiofiles.open(file_handler.baseFilename, "r", encoding="utf-8") as fp:
        await fp.seek(seek)
        data = await fp.read()
    return PlainTextResponse(data)
