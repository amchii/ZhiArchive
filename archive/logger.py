import logging
from logging.handlers import RotatingFileHandler

from archive.config import settings

verbose_formatter = logging.Formatter(
    "[%(levelname)s] [%(name)s] %(asctime)s %(filename)s %(levelno)s %(message)s"
)
logger = logging.getLogger("archive")
if settings.debug:
    # force log level to DEBUG
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())
else:
    logger.setLevel(settings.log_level)
file_handler = RotatingFileHandler(
    settings.log_dir.joinpath("archive.log"),
    maxBytes=1024 * 1024,  # 1MB
    backupCount=10,
    encoding="utf-8",
)
file_handler.setFormatter(verbose_formatter)
logger.addHandler(file_handler)
