import logging
from pathlib import Path

from pytest import LogCaptureFixture, MonkeyPatch

from archive.config import settings
from archive.logger import configure_logger


def test_business_logger_does_not_propagate_to_root(
    caplog: LogCaptureFixture,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 FastMCP 风格的 root handler 不会收到业务 INFO 日志。"""
    monkeypatch.setattr(settings, "log_dir", tmp_path)
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "log_level", "INFO")
    logger = configure_logger("isolated-business-test")
    caplog.set_level(logging.INFO)

    try:
        logger.info("business log should stay in file")
        for handler in logger.handlers:
            handler.flush()

        assert "business log should stay in file" not in caplog.text
        assert "business log should stay in file" in (
            tmp_path / "isolated-business-test.log"
        ).read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
