import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from archive.core.archiver import Archiver, TextArchive, ZhihuContentWorker
from archive.core.base import TargetType
from archive.core.reader import (
    ReaderAuthStateError,
    ReaderError,
    ReaderJob,
    ReaderRequestCancelledError,
    ReaderWorker,
)


def make_archive() -> TextArchive:
    """构造 Reader 测试使用的正文结果。"""
    return TextArchive(
        title="测试回答",
        url="https://www.zhihu.com/question/1/answer/2",
        author="author",
        author_url="https://www.zhihu.com/people/author",
        published_at="2026-07-17",
        updated_at="2026-07-17",
        target_type="回答",
        html="<p>正文</p>",
        markdown="正文",
    )


def test_reader_and_archiver_share_content_base_without_inheriting_each_other() -> None:
    """验证 Reader 与 Archiver 仅通过共同内容基类复用能力。"""
    assert issubclass(ReaderWorker, ZhihuContentWorker)
    assert issubclass(Archiver, ZhihuContentWorker)
    assert not issubclass(ReaderWorker, Archiver)
    assert {config.name for config in ReaderWorker.configurable}.isdisjoint(
        {"screenshot_max_page_scroll_height", "save_type"}
    )


@pytest.mark.asyncio
async def test_reader_uses_independent_context_without_writing_state(tmp_path) -> None:
    """验证 Reader 每次读取创建并关闭独立 Context，且不回写 state。"""
    state_path = tmp_path / "zhihu.state.json"
    worker = ReaderWorker(init_state_path=state_path)
    worker.auth_state.status = AsyncMock(
        return_value={"configured": True, "valid": True, "error": None}
    )
    worker.global_configurator.load_to_worker = AsyncMock()
    browser = MagicMock()
    context = MagicMock()
    page = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    context.close = AsyncMock()
    worker._ensure_browser = AsyncMock(return_value=browser)
    worker.init_context = AsyncMock()
    worker.new_page = AsyncMock(return_value=page)
    page.route = AsyncMock()
    worker.goto = AsyncMock()

    async def fill_metadata(
        _page: object,
        target: dict[str, object],
        target_type: TargetType,
    ) -> None:
        """为测试目标补充标题并校验识别类型。"""
        assert target_type == TargetType.ANSWER
        target["title"] = "测试回答"

    worker.fill_target_metadata = AsyncMock(side_effect=fill_metadata)
    worker.extract_text_archive = AsyncMock(return_value=make_archive())

    result = await worker._read_content(
        "https://www.zhihu.com/question/1/answer/2",
        headless=True,
    )

    assert result["markdown"] == "正文"
    browser.new_context.assert_awaited_once_with(
        storage_state=state_path,
        locale="zh-CN",
        user_agent=pytest.importorskip("archive.env").user_agent,
    )
    context.close.assert_awaited_once()
    assert not hasattr(context, "storage_state") or not context.storage_state.called


@pytest.mark.asyncio
async def test_reader_rejects_missing_auth_state(tmp_path) -> None:
    """验证 Reader 不会在登录态不可用时启动 Browser。"""
    worker = ReaderWorker(init_state_path=tmp_path / "missing.json")
    worker.auth_state.status = AsyncMock(
        return_value={
            "configured": False,
            "valid": False,
            "error": None,
        }
    )
    worker._ensure_browser = AsyncMock()

    with pytest.raises(ReaderAuthStateError):
        await worker._read_content(
            "https://www.zhihu.com/question/1/answer/2",
            headless=True,
        )

    worker._ensure_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_reader_execution_timeout_cancels_browser_work() -> None:
    """验证 Reader 到达截止时间时会取消实际读取协程。"""
    worker = ReaderWorker()
    cancelled = asyncio.Event()

    async def slow_read(_url: str, headless: bool) -> TextArchive:
        """模拟直到被 Reader 超时取消的浏览器读取。"""
        assert headless is True
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return make_archive()

    worker._read_content = AsyncMock(side_effect=slow_read)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[TextArchive] = loop.create_future()
    job = ReaderJob(
        url="https://www.zhihu.com/question/1/answer/2",
        future=future,
        deadline=loop.time() + 0.01,
    )

    with pytest.raises(ReaderError, match="超时"):
        await worker._execute_job(job, headless=True)

    assert cancelled.is_set()
    future.cancel()


@pytest.mark.asyncio
async def test_reader_caller_cancellation_stops_active_browser_work() -> None:
    """验证调用方取消后，已经开始的浏览器读取也会立即停止。"""
    worker = ReaderWorker()
    worker._ready.set()
    read_started = asyncio.Event()
    read_cancelled = asyncio.Event()

    async def slow_read(_url: str, headless: bool) -> TextArchive:
        """模拟正在执行且只能通过取消结束的浏览器读取。"""
        assert headless is True
        read_started.set()
        try:
            await asyncio.Future()
        finally:
            read_cancelled.set()

    worker._read_content = AsyncMock(side_effect=slow_read)
    submit_task = asyncio.create_task(
        worker.submit(
            "https://www.zhihu.com/question/1/answer/2",
            timeout=30,
        )
    )
    job = await worker.queue.get()
    execution_task = asyncio.create_task(worker._execute_job(job, headless=True))
    await read_started.wait()

    submit_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit_task
    with pytest.raises(ReaderRequestCancelledError):
        await asyncio.wait_for(execution_task, timeout=1)

    assert job.cancel_event.is_set()
    assert read_cancelled.is_set()
    worker.queue.task_done()
