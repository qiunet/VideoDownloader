"""应用日志：写入用户目录，便于排查 Cookie / 下载等问题。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from bootstrap import user_data_dir

_CONFIGURED = False
_LOG_DIR: Path | None = None


def log_dir() -> Path:
    global _LOG_DIR
    if _LOG_DIR is None:
        _LOG_DIR = user_data_dir() / "logs"
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


def setup_app_logging(*, level: int = logging.DEBUG) -> Path:
    """配置 VideoDownload / douyin_adapter / f2 日志到用户目录。"""
    global _CONFIGURED
    directory = log_dir()
    if _CONFIGURED:
        return directory

    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    log_file = directory / f"VideoDownload-{timestamp}.log"
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    for name in ("VideoDownload", "douyin_adapter", "f2"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.handlers.clear()
        logger.addHandler(file_handler)
        logger.propagate = False

    _CONFIGURED = True
    logging.getLogger("VideoDownload").info(
        "日志已初始化 platform=%s log_file=%s",
        sys.platform,
        log_file,
    )
    return directory


def log_file_hint() -> str:
    directory = log_dir()
    files = sorted(directory.glob("VideoDownload-*.log"), reverse=True)
    if files:
        return f"\n\n详细日志：{files[0]}"
    return f"\n\n日志目录：{directory}"
