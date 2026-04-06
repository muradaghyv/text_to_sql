"""
Centralised logger for the text-to-SQL API.

Writes to both the console and a rotating log file at logs/api.log
(10 MB per file, up to 5 backups kept).

Usage:
    from logger import get_logger
    logger = get_logger(__name__)
"""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")
LOG_FMT  = "%(asctime)s | %(levelname)-8s | %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _setup() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger("text_to_sql")
    if logger.handlers:          # already configured (e.g. on reload)
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(LOG_FMT, datefmt=DATE_FMT)

    # Console handler — INFO and above
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    # File handler — DEBUG and above, rotating at 10 MB, keep 5 backups
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


_root_logger = _setup()


def get_logger(name: str) -> logging.Logger:
    return _root_logger.getChild(name)
