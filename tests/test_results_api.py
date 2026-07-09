import pathlib
import threading

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response

from archive.api.endpoints.zhi import results


@pytest.fixture
def result_tree(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """
    创建包含 monitor、archiver 和隐藏任务目录的测试结果树。

    Args:
        tmp_path: Pytest 提供的临时目录。
        monkeypatch: Pytest 提供的属性替换工具。
    """
    monkeypatch.setattr(results.settings, "results_dir", tmp_path)
    activities_dir = tmp_path.joinpath("测试用户", "activities")
    archives_dir = tmp_path.joinpath(
        "测试用户", "archives", "2026", "07", "10", "赞同-回答-12345678"
    )
    tasks_dir = tmp_path.joinpath("测试用户", "tasks")
    activities_dir.joinpath("2026", "07", "10").mkdir(parents=True)
    archives_dir.mkdir(parents=True)
    tasks_dir.mkdir(parents=True)
    activities_dir.joinpath("动态快照.json").write_text(
        '[{"id": "activity-id"}]',
        encoding="utf-8",
    )
    activities_dir.joinpath("2026", "07", "10", "动态.jpeg").write_bytes(b"jpeg-data")
    archives_dir.joinpath("info.json").write_text(
        '{"title": "测试回答"}',
        encoding="utf-8",
    )
    archives_dir.joinpath("测试回答.md").write_text("# 测试回答", encoding="utf-8")
    archives_dir.joinpath("测试回答.html").write_text(
        "<html><body>测试回答</body></html>",
        encoding="utf-8",
    )
    tasks_dir.joinpath("task.json").write_text("[]", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_listing_only_exposes_activity_and_archive_roots(
    result_tree: pathlib.Path,
) -> None:
    """
    验证虚拟根目录只展示有结果的用户以及两个只读结果目录。

    Args:
        result_tree: 已创建的测试结果树。
    """
    root_response = Response()
    root_listing = await results.list_result_entries(
        response=root_response,
        path="",
        page=1,
        page_size=100,
    )
    assert [entry.name for entry in root_listing.entries] == ["测试用户"]
    assert root_response.headers["cache-control"] == "private, no-store"

    user_listing = await results.list_result_entries(
        response=Response(),
        path="测试用户",
        page=1,
        page_size=100,
    )
    assert {entry.name for entry in user_listing.entries} == {
        "activities",
        "archives",
    }
    assert all(
        entry.kind == results.ResultEntryKind.DIRECTORY
        for entry in user_listing.entries
    )


@pytest.mark.asyncio
async def test_listing_supports_chinese_paths_preview_types_and_pagination(
    result_tree: pathlib.Path,
) -> None:
    """
    验证中文路径、文件预览类型和目录分页信息。

    Args:
        result_tree: 已创建的测试结果树。
    """
    first_page = await results.list_result_entries(
        response=Response(),
        path="测试用户/activities",
        page=1,
        page_size=1,
    )
    assert first_page.total == 2
    assert len(first_page.entries) == 1
    assert first_page.entries[0].kind == results.ResultEntryKind.DIRECTORY

    second_page = await results.list_result_entries(
        response=Response(),
        path="测试用户/activities",
        page=2,
        page_size=1,
    )
    assert second_page.entries[0].name == "动态快照.json"
    assert second_page.entries[0].preview_type == results.ResultPreviewType.JSON
    assert second_page.parent == "测试用户"


@pytest.mark.asyncio
async def test_text_html_image_preview_and_download_responses(
    result_tree: pathlib.Path,
) -> None:
    """
    验证文本、HTML、图片预览和附件下载响应的安全头。

    Args:
        result_tree: 已创建的测试结果树。
    """
    text_response = await results.preview_result_file(
        path="测试用户/activities/动态快照.json"
    )
    assert isinstance(text_response, PlainTextResponse)
    assert "activity-id" in text_response.body.decode("utf-8")
    assert text_response.headers["x-content-type-options"] == "nosniff"
    assert text_response.headers["cache-control"] == "private, no-store"

    archive_root = "测试用户/archives/2026/07/10/赞同-回答-12345678"
    html_response = await results.preview_result_file(
        path=f"{archive_root}/测试回答.html"
    )
    assert isinstance(html_response, FileResponse)
    assert "sandbox" in html_response.headers["content-security-policy"]
    assert html_response.headers["content-disposition"].startswith("inline;")

    image_response = await results.preview_result_file(
        path="测试用户/activities/2026/07/10/动态.jpeg"
    )
    assert isinstance(image_response, FileResponse)
    assert image_response.headers["content-disposition"].startswith("inline;")

    download_response = await results.download_result_file(
        path=f"{archive_root}/测试回答.md"
    )
    assert isinstance(download_response, FileResponse)
    assert download_response.headers["content-disposition"].startswith("attachment;")
    assert download_response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_result_paths_reject_traversal_tasks_and_symlinks(
    result_tree: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """
    验证路径穿越、任务目录和符号链接均无法访问。

    Args:
        result_tree: 已创建的测试结果树。
        tmp_path: Pytest 提供的临时目录。
    """
    for path in ("../secret", "/etc/passwd", "测试用户/tasks"):
        with pytest.raises(HTTPException) as exc_info:
            await results.list_result_entries(
                response=Response(),
                path=path,
                page=1,
                page_size=100,
            )
        assert exc_info.value.status_code in {400, 404}

    outside_file = tmp_path.parent.joinpath("results-outside.txt")
    outside_file.write_text("outside", encoding="utf-8")
    link_path = result_tree.joinpath("测试用户", "activities", "outside.txt")
    link_path.symlink_to(outside_file)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await results.preview_result_file(path="测试用户/activities/outside.txt")
        assert exc_info.value.status_code == 404
    finally:
        outside_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_large_text_requires_download(
    result_tree: pathlib.Path,
) -> None:
    """
    验证超过预览上限的文本文件不会被整体读入内存。

    Args:
        result_tree: 已创建的测试结果树。
    """
    large_file = result_tree.joinpath("测试用户", "activities", "large.txt")
    large_file.write_bytes(b"x" * (results.MAX_TEXT_PREVIEW_SIZE + 1))

    with pytest.raises(HTTPException) as exc_info:
        await results.preview_result_file(path="测试用户/activities/large.txt")
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_missing_results_root_returns_empty_listing(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证尚未产生任何结果时根页面可以正常展示空目录。

    Args:
        tmp_path: Pytest 提供的临时目录。
        monkeypatch: Pytest 提供的属性替换工具。
    """
    missing_root = tmp_path.joinpath("missing-results")
    monkeypatch.setattr(results.settings, "results_dir", missing_root)

    listing = await results.list_result_entries(
        response=Response(),
        path="",
        page=1,
        page_size=100,
    )
    assert listing.total == 0
    assert listing.entries == []


@pytest.mark.asyncio
async def test_listing_scans_directory_outside_event_loop(
    result_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证目录扫描被放入工作线程，不阻塞 FastAPI 事件循环。

    Args:
        result_tree: 已创建的测试结果树。
        monkeypatch: Pytest 提供的属性替换工具。
    """
    main_thread_id = threading.get_ident()
    scan_thread_id: int | None = None
    original_scan = results.scan_result_directory_page

    def capture_scan_thread(
        target_path: pathlib.Path,
        relative_path: pathlib.PurePosixPath,
        page: int,
        page_size: int,
    ) -> tuple[int, list[results.ResultEntry]]:
        """记录目录扫描实际运行的线程。"""
        nonlocal scan_thread_id
        scan_thread_id = threading.get_ident()
        return original_scan(target_path, relative_path, page, page_size)

    monkeypatch.setattr(results, "scan_result_directory_page", capture_scan_thread)

    await results.list_result_entries(
        response=Response(),
        path="测试用户/activities",
        page=1,
        page_size=100,
    )

    assert scan_thread_id is not None
    assert scan_thread_id != main_thread_id


@pytest.mark.asyncio
async def test_listing_only_builds_models_for_requested_page(
    result_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证分页切片发生在 Pydantic 响应模型构建之前。

    Args:
        result_tree: 已创建的测试结果树。
        monkeypatch: Pytest 提供的属性替换工具。
    """
    model_count = 0
    original_builder = results.ScannedResultEntry.to_result_entry

    def count_built_model(
        entry: results.ScannedResultEntry,
    ) -> results.ResultEntry:
        """记录实际构建的分页响应模型数量。"""
        nonlocal model_count
        model_count += 1
        return original_builder(entry)

    monkeypatch.setattr(
        results.ScannedResultEntry,
        "to_result_entry",
        count_built_model,
    )

    listing = await results.list_result_entries(
        response=Response(),
        path="测试用户/activities",
        page=1,
        page_size=1,
    )

    assert len(listing.entries) == 1
    assert model_count == 1
