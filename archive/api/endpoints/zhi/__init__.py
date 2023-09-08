from fastapi import APIRouter, Depends
from pydantic import BaseModel

from archive.api.security import verify_user_from_cookie

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])


class PauseStatus(BaseModel):
    pause: bool


from . import core, login  # noqa: E402

router.include_router(
    login.router,
    prefix="/login",
)
router.include_router(
    core.router,
    prefix="/core",
)
