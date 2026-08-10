import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import archive.core.reader as reader_module
from archive.core.archiver import Archiver, TextArchive, ZhihuContentWorker
from archive.core.base import TargetType
from archive.core.profile import (
    ProfileContentType,
    ProfilePage,
    ProfileRateLimitError,
)
from archive.core.reader import (
    ProfileReaderJob,
    ReaderAuthStateError,
    ReaderError,
    ReaderJob,
    ReaderProfileCooldownError,
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


@pytest.mark.asyncio
async def test_reader_profile_uses_independent_context(monkeypatch, tmp_path) -> None:
    """验证个人列表复用 Reader Browser，但每次关闭独立 Context。"""
    worker = ReaderWorker(init_state_path=tmp_path / "zhihu.state.json")
    worker.auth_state.status = AsyncMock(
        return_value={"configured": True, "valid": True, "error": None}
    )
    browser = MagicMock()
    context = MagicMock()
    context.close = AsyncMock()
    worker._ensure_browser = AsyncMock(return_value=browser)
    worker._new_reader_context = AsyncMock(return_value=context)
    expected = ProfilePage(
        people="target-user",
        content_type=ProfileContentType.ANSWER,
        items=[],
        offset=0,
        limit=20,
        total=0,
        has_more=False,
        next_cursor=None,
    )
    read_page = AsyncMock(return_value=expected)
    monkeypatch.setattr(reader_module, "read_profile_page", read_page)

    result = await worker._read_profile(
        content_type=ProfileContentType.ANSWER,
        people="target-user",
        offset=0,
        limit=20,
        collection_id=None,
        headless=True,
    )
    cached_result = await worker._read_profile(
        content_type=ProfileContentType.ANSWER,
        people="target-user",
        offset=0,
        limit=20,
        collection_id=None,
        headless=True,
    )

    assert result is expected
    assert cached_result == expected
    assert cached_result is not expected
    worker._ensure_browser.assert_awaited_once_with(True)
    worker._new_reader_context.assert_awaited_once_with(browser)
    read_page.assert_awaited_once_with(
        context.request,
        content_type=ProfileContentType.ANSWER,
        people="target-user",
        offset=0,
        limit=20,
        collection_id=None,
        timeout=worker.page_default_timeout,
    )
    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reader_profile_applies_minimum_interval_and_jitter(
    monkeypatch,
) -> None:
    """验证连续个人列表请求会等待最小间隔及随机抖动。"""
    worker = ReaderWorker(
        profile_request_min_interval_seconds=2,
        profile_request_jitter_seconds=1,
    )
    worker._profile_last_request_at = asyncio.get_running_loop().time()
    sleep = AsyncMock()
    monkeypatch.setattr(reader_module.random, "uniform", lambda _start, _end: 0.5)
    monkeypatch.setattr(reader_module.asyncio, "sleep", sleep)

    await worker._wait_for_profile_request_slot()

    sleep.assert_awaited_once()
    assert sleep.await_args.args[0] == pytest.approx(2.5, abs=0.1)


@pytest.mark.asyncio
async def test_reader_profile_opens_circuit_after_rate_limit(
    monkeypatch,
    tmp_path,
) -> None:
    """验证 403/429 后续请求会在本地冷却期内直接失败。"""
    worker = ReaderWorker(
        init_state_path=tmp_path / "zhihu.state.json",
        profile_cache_ttl_seconds=0,
        profile_cooldown_base_seconds=60,
        profile_cooldown_max_seconds=300,
    )
    worker.auth_state.status = AsyncMock(
        return_value={"configured": True, "valid": True, "error": None}
    )
    browser = MagicMock()
    context = MagicMock()
    context.close = AsyncMock()
    worker._ensure_browser = AsyncMock(return_value=browser)
    worker._new_reader_context = AsyncMock(return_value=context)
    read_page = AsyncMock(
        side_effect=ProfileRateLimitError(
            "请求过于频繁",
            status=429,
            retry_after=120,
        )
    )
    monkeypatch.setattr(reader_module, "read_profile_page", read_page)

    with pytest.raises(ProfileRateLimitError):
        await worker._read_profile(
            content_type=ProfileContentType.PIN,
            people="target-user",
            offset=0,
            limit=20,
            collection_id=None,
            headless=True,
        )
    with pytest.raises(ReaderProfileCooldownError, match="冷却"):
        await worker._read_profile(
            content_type=ProfileContentType.PIN,
            people="target-user",
            offset=20,
            limit=20,
            collection_id=None,
            headless=True,
        )

    worker._ensure_browser.assert_awaited_once_with(True)
    worker._new_reader_context.assert_awaited_once_with(browser)
    read_page.assert_awaited_once()
    context.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_submit_profile_uses_shared_reader_queue() -> None:
    """验证个人列表请求进入现有 Reader 有界队列并按类型分派。"""
    worker = ReaderWorker()
    worker._ready.set()
    expected = ProfilePage(
        people="target-user",
        content_type=ProfileContentType.PIN,
        items=[],
        offset=0,
        limit=20,
        total=0,
        has_more=False,
        next_cursor=None,
    )
    worker._read_profile = AsyncMock(return_value=expected)
    submit_task = asyncio.create_task(
        worker.submit_profile(
            content_type=ProfileContentType.PIN,
            people="target-user",
            offset=0,
            limit=20,
            collection_id=None,
            timeout=30,
        )
    )
    job = await worker.queue.get()

    assert isinstance(job, ProfileReaderJob)
    result = await worker._execute_job(job, headless=True)
    job.future.set_result(result)
    assert await submit_task is expected
    worker._read_profile.assert_awaited_once_with(
        content_type=ProfileContentType.PIN,
        people="target-user",
        offset=0,
        limit=20,
        collection_id=None,
        headless=True,
    )
    worker.queue.task_done()
