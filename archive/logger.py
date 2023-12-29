import logging
from logging.handlers import RotatingFileHandler

from archive.config import settings

verbose_formatter = logging.Formatter(
    "[%(levelname)s] [%(name)s] %(asctime)s %(filename)s %(lineno)s %(message)s"
)


def configure_logger(name, max_bytes=1024 * 1024 * 5):
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


configure_logger("default")
configure_logger("archiver")
configure_logger("monitor")
configure_logger("login_worker")
