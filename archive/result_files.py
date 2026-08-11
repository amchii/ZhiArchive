import pathlib
from enum import Enum

VISIBLE_RESULT_DIRS = frozenset({"activities", "archives"})
IMAGE_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".gif", ".webp"})
TEXT_PREVIEW_SUFFIXES = {
    ".json": "json",
    ".md": "markdown",
    ".txt": "text",
    ".log": "text",
}
MAX_TEXT_PREVIEW_SIZE = 1024 * 1024


class ResultPreviewType(str, Enum):
    """结果文件可用的预览类型。"""

    IMAGE = "image"
    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"


class ResultPathError(Exception):
    """表示结果路径不合法、越界或不存在。"""

    def __init__(self, status_code: int, message: str) -> None:
        """创建可转换为不同协议错误的结果路径异常。

        Args:
            status_code: 建议使用的 HTTP 状态码。
            message: 面向调用方的错误消息。
        """
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def normalize_result_path(raw_path: str) -> pathlib.PurePosixPath:
    """标准化结果相对路径并拒绝越界片段。

    Args:
        raw_path: 相对于结果根目录的路径。
    """
    if "\x00" in raw_path:
        raise ResultPathError(400, "结果路径不合法")
    relative_path = pathlib.PurePosixPath(raw_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ResultPathError(400, "结果路径不合法")
    return relative_path


def validate_result_scope(relative_path: pathlib.PurePosixPath) -> None:
    """限制结果路径只能位于用户的 activities 或 archives 目录。

    Args:
        relative_path: 已标准化的结果相对路径。
    """
    parts = relative_path.parts
    if len(parts) >= 2 and parts[1] not in VISIBLE_RESULT_DIRS:
        raise ResultPathError(404, "结果路径不存在")


def reject_symlink_path(
    root: pathlib.Path,
    relative_path: pathlib.PurePosixPath,
) -> None:
    """拒绝结果路径任一层级中的符号链接。

    Args:
        root: 已解析的结果根目录。
        relative_path: 已标准化的结果相对路径。
    """
    current_path = root
    for part in relative_path.parts:
        current_path = current_path.joinpath(part)
        if current_path.is_symlink():
            raise ResultPathError(404, "结果路径不存在")


def resolve_result_file_path(
    raw_path: str,
    results_root: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.PurePosixPath]:
    """在结果根目录内安全解析一个已存在的相对路径。

    Args:
        raw_path: 相对于结果根目录的路径。
        results_root: API 或 MCP 当前使用的结果根目录。

    Returns:
        解析后的文件系统路径和标准化相对路径。
    """
    relative_path = normalize_result_path(raw_path)
    validate_result_scope(relative_path)
    root = results_root.expanduser().resolve()
    if not root.exists():
        if not relative_path.parts:
            return root, relative_path
        raise ResultPathError(404, "结果路径不存在")
    reject_symlink_path(root, relative_path)
    try:
        target_path = root.joinpath(*relative_path.parts).resolve(strict=True)
        target_path.relative_to(root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise ResultPathError(404, "结果路径不存在") from error
    return target_path, relative_path


def get_preview_type(path: pathlib.Path) -> ResultPreviewType | None:
    """根据扩展名返回结果文件的预览类型。

    Args:
        path: 待判断的结果文件路径。
    """
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return ResultPreviewType.IMAGE
    if suffix == ".html":
        return ResultPreviewType.HTML
    preview_type = TEXT_PREVIEW_SUFFIXES.get(suffix)
    return ResultPreviewType(preview_type) if preview_type else None
