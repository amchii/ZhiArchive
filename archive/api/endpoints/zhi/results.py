import asyncio
import mimetypes
import os
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from archive.api.render import templates
from archive.api.security import verify_user_from_cookie
from archive.config import settings
from archive.result_files import (
    MAX_TEXT_PREVIEW_SIZE,
    VISIBLE_RESULT_DIRS,
    ResultPathError,
    ResultPreviewType,
    get_preview_type,
    resolve_result_file_path,
)

router = APIRouter(dependencies=[Depends(verify_user_from_cookie)])
public_router = APIRouter()

PRIVATE_NO_STORE_HEADERS = {"Cache-Control": "private, no-store"}
PRIVATE_FILE_HEADERS = {
    **PRIVATE_NO_STORE_HEADERS,
    "X-Content-Type-Options": "nosniff",
}


class ResultEntryKind(str, Enum):
    """结果目录条目类型。"""

    DIRECTORY = "directory"
    FILE = "file"


class ResultEntry(BaseModel):
    """结果文件浏览器中的单个目录条目。"""

    name: str
    path: str
    kind: ResultEntryKind
    size: int | None
    modified_at: datetime
    mime_type: str | None
    preview_type: ResultPreviewType | None


class ResultDirectoryListing(BaseModel):
    """结果目录分页列表。"""

    path: str
    parent: str | None
    page: int
    page_size: int
    total: int
    entries: list[ResultEntry]


@dataclass(frozen=True)
class ScannedResultEntry:
    """尚未转换为 API 模型的轻量目录扫描结果。"""

    name: str
    path: str
    kind: ResultEntryKind
    size: int | None
    modified_timestamp: float

    def to_result_entry(self) -> ResultEntry:
        """将当前扫描结果转换为 API 响应模型。"""
        is_directory = self.kind == ResultEntryKind.DIRECTORY
        return ResultEntry(
            name=self.name,
            path=self.path,
            kind=self.kind,
            size=self.size,
            modified_at=datetime.fromtimestamp(
                self.modified_timestamp,
                tz=timezone.utc,
            ),
            mime_type=None if is_directory else mimetypes.guess_type(self.name)[0],
            preview_type=(
                None if is_directory else get_preview_type(pathlib.Path(self.name))
            ),
        )


def resolve_result_path(raw_path: str) -> tuple[pathlib.Path, pathlib.PurePosixPath]:
    """
    解析并校验一个已存在的结果路径。

    Args:
        raw_path: 相对于结果根目录的 URL 路径。

    Returns:
        解析后的文件系统路径和标准化相对路径。
    """
    try:
        return resolve_result_file_path(raw_path, settings.results_dir)
    except ResultPathError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.message,
        ) from error


def is_visible_entry(
    entry: os.DirEntry[str],
    relative_path: pathlib.PurePosixPath,
) -> bool:
    """
    判断一个目录条目是否应出现在结果浏览器中。

    Args:
        entry: 当前目录中的文件系统条目。
        relative_path: 当前正在浏览的相对目录。
    """
    if entry.name.startswith(".") or entry.is_symlink():
        return False
    if not relative_path.parts:
        if not entry.is_dir(follow_symlinks=False):
            return False
        entry_path = pathlib.Path(entry.path)
        return any(
            entry_path.joinpath(name).is_dir()
            and not entry_path.joinpath(name).is_symlink()
            for name in VISIBLE_RESULT_DIRS
        )
    if len(relative_path.parts) == 1:
        return entry.name in VISIBLE_RESULT_DIRS and entry.is_dir(follow_symlinks=False)
    return entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)


def build_scanned_result_entry(
    entry: os.DirEntry[str],
    relative_path: pathlib.PurePosixPath,
) -> ScannedResultEntry | None:
    """
    将文件系统目录条目转换为轻量扫描结果。

    Args:
        entry: 当前目录中的文件系统条目。
        relative_path: 当前正在浏览的相对目录。
    """
    try:
        stat_result = entry.stat(follow_symlinks=False)
        is_directory = entry.is_dir(follow_symlinks=False)
    except OSError:
        return None
    entry_path = relative_path.joinpath(entry.name)
    return ScannedResultEntry(
        name=entry.name,
        path=entry_path.as_posix(),
        kind=(ResultEntryKind.DIRECTORY if is_directory else ResultEntryKind.FILE),
        size=None if is_directory else stat_result.st_size,
        modified_timestamp=stat_result.st_mtime,
    )


def scan_result_directory_page(
    target_path: pathlib.Path,
    relative_path: pathlib.PurePosixPath,
    page: int,
    page_size: int,
) -> tuple[int, list[ResultEntry]]:
    """
    扫描、排序并转换结果目录中的指定分页，不执行递归扫描。

    Args:
        target_path: 当前目录的文件系统路径。
        relative_path: 当前目录的结果相对路径。
        page: 从 1 开始的页码。
        page_size: 每页条目数。
    """
    if not target_path.exists():
        return 0, []
    try:
        with os.scandir(target_path) as directory_entries:
            entries = [
                result_entry
                for entry in directory_entries
                if is_visible_entry(entry, relative_path)
                if (
                    result_entry := build_scanned_result_entry(
                        entry,
                        relative_path,
                    )
                )
                is not None
            ]
    except OSError as error:
        raise HTTPException(status_code=404, detail="结果目录无法读取") from error
    entries.sort(
        key=lambda entry: (
            entry.kind != ResultEntryKind.DIRECTORY,
            -entry.modified_timestamp,
            entry.name.casefold(),
        )
    )
    total = len(entries)
    start = (page - 1) * page_size
    return total, [
        entry.to_result_entry() for entry in entries[start : start + page_size]
    ]


def get_parent_path(relative_path: pathlib.PurePosixPath) -> str | None:
    """
    返回目录面包屑所需的父路径。

    Args:
        relative_path: 当前结果相对路径。
    """
    if not relative_path.parts:
        return None
    if len(relative_path.parts) == 1:
        return ""
    return relative_path.parent.as_posix()


@router.get("/api/entries", response_model=ResultDirectoryListing)
async def list_result_entries(
    response: Response,
    path: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ResultDirectoryListing:
    """
    分页返回结果目录中的直接子项。

    Args:
        response: 用于写入结果数据缓存控制头的响应对象。
        path: 相对于结果根目录的目录路径。
        page: 从 1 开始的页码。
        page_size: 每页条目数，最多 200。
    """
    response.headers.update(PRIVATE_NO_STORE_HEADERS)
    target_path, relative_path = resolve_result_path(path)
    if target_path.exists() and not target_path.is_dir():
        raise HTTPException(status_code=400, detail="结果路径不是目录")
    total, entries = await asyncio.to_thread(
        scan_result_directory_page,
        target_path,
        relative_path,
        page,
        page_size,
    )
    return ResultDirectoryListing(
        path=relative_path.as_posix() if relative_path.parts else "",
        parent=get_parent_path(relative_path),
        page=page,
        page_size=page_size,
        total=total,
        entries=entries,
    )


@router.get("/api/preview")
async def preview_result_file(path: str) -> Response:
    """
    以安全方式返回可预览的结果文件。

    Args:
        path: 相对于结果根目录的文件路径。
    """
    target_path, _relative_path = resolve_result_path(path)
    if not target_path.is_file():
        raise HTTPException(status_code=400, detail="结果路径不是文件")
    preview_type = get_preview_type(target_path)
    if preview_type == ResultPreviewType.IMAGE:
        return FileResponse(
            target_path,
            filename=target_path.name,
            content_disposition_type="inline",
            headers=PRIVATE_FILE_HEADERS,
        )
    if preview_type == ResultPreviewType.HTML:
        return FileResponse(
            target_path,
            filename=target_path.name,
            content_disposition_type="inline",
            media_type="text/html",
            headers={
                **PRIVATE_FILE_HEADERS,
                "Content-Security-Policy": (
                    "sandbox; default-src 'none'; img-src https: data:; "
                    "style-src 'unsafe-inline'"
                ),
            },
        )
    if preview_type in {
        ResultPreviewType.JSON,
        ResultPreviewType.MARKDOWN,
        ResultPreviewType.TEXT,
    }:
        try:
            size = target_path.stat().st_size
        except OSError as error:
            raise HTTPException(status_code=404, detail="结果文件不存在") from error
        if size > MAX_TEXT_PREVIEW_SIZE:
            raise HTTPException(status_code=413, detail="文件过大，请下载后查看")
        try:
            async with aiofiles.open(target_path, "r", encoding="utf-8") as fp:
                content = await fp.read()
        except (OSError, UnicodeDecodeError) as error:
            raise HTTPException(status_code=422, detail="结果文件无法预览") from error
        return PlainTextResponse(
            content,
            headers=PRIVATE_FILE_HEADERS,
        )
    raise HTTPException(status_code=415, detail="不支持预览此文件")


@router.get("/api/download")
async def download_result_file(path: str) -> FileResponse:
    """
    以附件形式下载一个结果文件。

    Args:
        path: 相对于结果根目录的文件路径。
    """
    target_path, _relative_path = resolve_result_path(path)
    if not target_path.is_file():
        raise HTTPException(status_code=400, detail="结果路径不是文件")
    return FileResponse(
        target_path,
        filename=target_path.name,
        content_disposition_type="attachment",
        headers=PRIVATE_FILE_HEADERS,
    )


@public_router.get("", response_class=HTMLResponse, name="zhi:results_view")
async def results_view(request: Request) -> HTMLResponse:
    """
    渲染结果文件浏览器页面。

    Args:
        request: 当前 FastAPI 请求。
    """
    return templates.TemplateResponse(
        request,
        "results.html",
        context={
            "config_url": str(request.url_for("zhi:config_view")),
        },
    )
