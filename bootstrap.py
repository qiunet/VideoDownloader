"""启动引导：优先加载用户目录中热更新的 yt-dlp。"""

from __future__ import annotations

import sys
from pathlib import Path


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    path = base / "VideoDownload"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ytdlp_runtime_dir() -> Path:
    return user_data_dir() / "yt_dlp" / "runtime"


def apply_ytdlp_override() -> None:
    runtime = ytdlp_runtime_dir()
    if runtime.is_dir() and (runtime / "yt_dlp").is_dir():
        runtime_str = str(runtime)
        if runtime_str not in sys.path:
            sys.path.insert(0, runtime_str)
