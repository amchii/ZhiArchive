import logging
from logging.handlers import RotatingFileHandler

from archive.config import settings

verbose_formatter = logging.Formatter(
    "[%(levelname)s] [%(name)s] %(asctime)s %(filename)s %(lineno)s %(message)s"
)


def configure_logger(
    name: str,
    max_bytes: int = 1024 * 1024 * 5,
) -> logging.Logger:
    """配置独立文件日志，并阻止记录重复传播到 root logger。

    Args:
        name: 待配置的 logger 名称。
        max_bytes: 单个轮转日志文件的最大字节数。
    """
    logger = logging.getLogger(name)

    file_handler = RotatingFileHandler(
        settings.log_dir.joinpath(f"{name}.log"),
        maxBytes=max_bytes,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(verbose_formatter)
    logger.addHandler(file_handler)

    if settings.debug:
        # force log level to DEBUG
        logger.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(verbose_formatter)
        logger.addHandler(console_handler)
    else:
        logger.setLevel(settings.log_level)
    # FastMCP 会为 root logger 安装 console handler；业务 logger 已有自己的
    # handler，不应再向 root 传播，否则非 debug 模式也会输出到终端。
    logger.propagate = False
    return logger


configure_logger("default")
configure_logger("archiver")
configure_logger("monitor")
configure_logger("login_worker")
configure_logger("reader")
