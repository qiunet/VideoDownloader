#!/usr/bin/env python3
"""VideoDownload - yt-dlp GUI 客户端"""

from __future__ import annotations

import bootstrap

bootstrap.apply_ytdlp_override()

import os
import queue
import re
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app_log import setup_app_logging
from updater import (
    UpdateResult,
    UpdateWorker,
    VersionInfo,
    apply_pending_restart,
    format_update_summary,
    is_frozen,
)
from version import APP_VERSION

import yt_dlp
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "VideoDownload"
DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads")

YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extractor_args": {"youtube": {"player_client": ["default", "-android_sdkless"]}},
}

# 部分站点需要额外请求头，否则会触发反爬（如 B 站 412）
SITE_HEADERS: dict[str, dict[str, str]] = {
    "bilibili.com": {
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    },
    "b23.tv": {
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    },
}

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text).strip()


def format_percent(downloaded: int | None, total: int | None) -> str:
    if downloaded and total:
        return f"{(downloaded / total) * 100:.1f}%"
    return ""


def format_speed(speed: float | int | None) -> str:
    if not speed:
        return ""
    value = float(speed)
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B/s":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return ""


def format_eta(seconds: int | float | None) -> str:
    if seconds is None:
        return ""
    sec = int(seconds)
    minutes, sec = divmod(sec, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


@dataclass
class FormatOption:
    format_selector: str
    quality: str


@dataclass
class DownloadTask:
    task_id: str
    url: str
    title: str = "准备中..."
    status: str = "等待中"
    progress: str = "0%"
    speed: str = ""
    eta: str = ""
    error: str = ""


@dataclass
class FormatFetchResult:
    ok: bool
    url: str = ""
    title: str = ""
    formats: list[FormatOption] = field(default_factory=list)
    error: str = ""


@dataclass
class PendingTask:
    task_id: str
    url: str
    title: str
    formats: list[FormatOption]


def find_ffmpeg() -> str | None:
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bundled = resource_path(f"bin/{name}")
    if bundled.is_file():
        if sys.platform != "win32":
            bundled.chmod(bundled.stat().st_mode | 0o111)
        return str(bundled)
    return shutil.which("ffmpeg")


def has_ffmpeg() -> bool:
    return find_ffmpeg() is not None


def format_needs_merge(format_selector: str) -> bool:
    from douyin_adapter import is_douyin_format

    if is_douyin_format(format_selector):
        return False
    return "+" in format_selector or format_selector.startswith(("bv", "bestvideo"))


def build_ydl_opts(url: str, **extra) -> dict:
    """按站点补充 yt-dlp 参数（请求头等）。"""
    opts = {**YDL_BASE_OPTS, **extra}
    headers = dict(opts.get("http_headers") or {})
    for domain, site_headers in SITE_HEADERS.items():
        if domain in url:
            headers.update(site_headers)
            break
    if headers:
        opts["http_headers"] = headers
    return opts


def format_selector_for_fmt(fmt: dict, *, allow_merge: bool) -> str:
    fmt_id = str(fmt["format_id"])
    if fmt.get("acodec") not in (None, "none"):
        return fmt_id
    if allow_merge:
        return f"{fmt_id}+bestaudio/best"
    return fmt_id


def pick_best_video_at_height(formats: list[dict], height: int) -> dict | None:
    same_height = [
        f
        for f in formats
        if f.get("height") == height and f.get("vcodec") not in (None, "none")
    ]
    if same_height:
        return max(same_height, key=lambda f: f.get("tbr") or 0)

    lower = [
        f
        for f in formats
        if (f.get("height") or 0) <= height and f.get("vcodec") not in (None, "none")
    ]
    if lower:
        return max(lower, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return None


def height_format_selector(height: int, *, allow_merge: bool) -> str:
    if allow_merge:
        return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    return f"best[height<={height}][ext=mp4]/best[height<={height}]"


def quality_label(height: int) -> str:
    if height >= 2160:
        return "4K"
    return f"{height}p"


def pick_default_quality_index(options: list[FormatOption]) -> int:
    """默认选 720p；没有则选最接近 720p 的分辨率。"""
    for idx, option in enumerate(options):
        if option.quality == "720p":
            return idx

    candidates: list[tuple[int, int, int]] = []
    for idx, option in enumerate(options):
        match = re.fullmatch(r"(\d+)p", option.quality)
        if not match:
            continue
        height = int(match.group(1))
        # key: 与720距离、是否低于720(优先高于/等于720)、原始顺序
        candidates.append((abs(height - 720), 1 if height < 720 else 0, idx))

    if candidates:
        return min(candidates)[2]
    return 0


def parse_formats(info: dict, *, allow_merge: bool) -> list[FormatOption]:
    options: list[FormatOption] = [
        FormatOption(
            format_selector="bestvideo+bestaudio/best" if allow_merge else "best[ext=mp4]/best",
            quality="最佳",
        )
    ]

    formats = info.get("formats") or []
    video_formats = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none") and f.get("height")
    ]
    if not allow_merge:
        video_formats = [
            f for f in video_formats if f.get("acodec") not in (None, "none")
        ]

    heights = sorted({int(f["height"]) for f in video_formats}, reverse=True)
    seen_selectors: set[str] = set()
    for height in heights:
        best_fmt = pick_best_video_at_height(video_formats, height)
        if best_fmt is not None:
            selector = format_selector_for_fmt(best_fmt, allow_merge=allow_merge)
        else:
            selector = height_format_selector(height, allow_merge=allow_merge)
        if selector in seen_selectors:
            continue
        seen_selectors.add(selector)
        options.append(
            FormatOption(
                format_selector=selector,
                quality=quality_label(height),
            )
        )

    audio_formats = [
        f
        for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    if audio_formats:
        best_audio = max(audio_formats, key=lambda f: f.get("abr") or 0)
        options.append(
            FormatOption(
                format_selector=str(best_audio["format_id"]),
                quality="仅音频",
            )
        )

    return options


class FormatFetcher:
    def __init__(
        self,
        on_done: Callable[[FormatFetchResult], None],
        *,
        allow_merge: bool,
    ) -> None:
        self._on_done = on_done
        self._allow_merge = allow_merge

    def fetch(self, url: str) -> None:
        thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        thread.start()

    def _run(self, url: str) -> None:
        try:
            from douyin_adapter import is_douyin_url, probe_url as probe_douyin_url

            if is_douyin_url(url):
                self._run_douyin(url)
                return
            with yt_dlp.YoutubeDL(build_ydl_opts(url)) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                self._on_done(FormatFetchResult(ok=False, url=url, error="无法解析视频信息"))
                return
            title = info.get("title") or url
            formats = parse_formats(info, allow_merge=self._allow_merge)
            if not formats:
                hint = ""
                if not self._allow_merge:
                    hint = "\n\n提示：安装 ffmpeg 后可下载更高清晰度。\nmacOS: brew install ffmpeg"
                self._on_done(
                    FormatFetchResult(
                        ok=False,
                        url=url,
                        error=f"未找到可下载的清晰度{hint}",
                    )
                )
                return
            self._on_done(
                FormatFetchResult(ok=True, url=url, title=title, formats=formats)
            )
        except Exception as exc:  # noqa: BLE001
            self._on_done(FormatFetchResult(ok=False, url=url, error=str(exc)))

    def _run_douyin(self, url: str) -> None:
        from douyin_adapter import probe_url as probe_douyin_url

        try:
            result = probe_douyin_url(url)
            formats = [
                FormatOption(f.format_selector, f.quality) for f in result.formats
            ]
            if not formats:
                self._on_done(
                    FormatFetchResult(ok=False, url=url, error="未找到可下载的清晰度")
                )
                return
            self._on_done(
                FormatFetchResult(
                    ok=True,
                    url=url,
                    title=result.title,
                    formats=formats,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._on_done(FormatFetchResult(ok=False, url=url, error=str(exc)))


class DownloadManager:
    def __init__(self, on_update: Callable[[DownloadTask], None]) -> None:
        self._on_update = on_update
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()

    def start_download(
        self,
        url: str,
        output_dir: str,
        format_selector: str,
        title: str = "",
        task_id: str | None = None,
    ) -> str:
        tid = task_id or str(uuid.uuid4())[:8]
        task = DownloadTask(task_id=tid, url=url, title=title or "准备中...")
        with self._lock:
            self._tasks[tid] = task
        self._on_update(task)

        thread = threading.Thread(
            target=self._run_download,
            args=(tid, url, output_dir, format_selector, title),
            daemon=True,
        )
        thread.start()
        return tid

    def _notify(self, task: DownloadTask) -> None:
        self._on_update(task)

    def _run_download(
        self,
        task_id: str,
        url: str,
        output_dir: str,
        format_selector: str,
        title: str,
    ) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if title:
                task.title = title

        def progress_hook(data: dict) -> None:
            with self._lock:
                t = self._tasks[task_id]
                if data.get("status") == "downloading":
                    t.status = "下载中"
                    downloaded = data.get("downloaded_bytes")
                    total = data.get("total_bytes") or data.get("total_bytes_estimate")
                    t.progress = format_percent(downloaded, total) or strip_ansi(
                        str(data.get("_percent_str", "0%"))
                    )
                    t.speed = format_speed(data.get("speed")) or strip_ansi(
                        str(data.get("_speed_str", ""))
                    )
                    t.eta = format_eta(data.get("eta")) or strip_ansi(
                        str(data.get("_eta_str", ""))
                    )
                    if data.get("info_dict", {}).get("title"):
                        t.title = data["info_dict"]["title"]
                elif data.get("status") == "finished":
                    t.status = "处理中"
                    t.progress = "100%"
                self._notify(t)

        try:
            with self._lock:
                task.status = "下载中"
                task.progress = "0%"
                self._notify(task)

            from douyin_adapter import is_douyin_format

            if is_douyin_format(format_selector):
                from douyin_adapter import download as download_douyin

                download_douyin(url, output_dir, format_selector, progress_hook)
            else:
                ydl_opts: dict = {
                    **build_ydl_opts(url),
                    "format": format_selector,
                    "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
                    "progress_hooks": [progress_hook],
                }

                ffmpeg = find_ffmpeg()
                if ffmpeg:
                    ydl_opts["ffmpeg_location"] = ffmpeg
                    ydl_opts["merge_output_format"] = "mp4"

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

            with self._lock:
                task.status = "完成"
                task.progress = "100%"
                task.speed = ""
                task.eta = ""
                self._notify(task)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                task.status = "失败"
                task.error = str(exc)
                self._notify(task)


class UpdateBridge(QObject):
    check_done = pyqtSignal(object, str)
    update_done = pyqtSignal(object)
    progress = pyqtSignal(str, object)


class MainWindow(QMainWindow):
    COL_TITLE = 0
    COL_STATUS = 1
    COL_QUALITY = 2
    COL_PROGRESS = 3
    COL_SPEED = 4
    COL_ETA = 5

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(900, 600)
        self.setMinimumSize(760, 480)

        icon_path = resource_path("assets/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._last_download_dir = DEFAULT_DOWNLOAD_DIR
        self._ui_queue: queue.Queue[DownloadTask] = queue.Queue()
        self._format_queue: queue.Queue[FormatFetchResult] = queue.Queue()
        self._task_rows: dict[str, int] = {}
        self._pending_tasks: dict[str, PendingTask] = {}
        self._row_full_titles: dict[int, str] = {}
        self._allow_merge = has_ffmpeg()

        self.manager = DownloadManager(on_update=self._enqueue_update)
        self.fetcher = FormatFetcher(
            on_done=self._enqueue_format_result,
            allow_merge=self._allow_merge,
        )

        self._build_ui()
        QTimer.singleShot(0, self._refresh_table_layout)

        if self._allow_merge:
            mode = "开发模式" if not is_frozen() else "就绪"
            self.status_bar.showMessage(
                f"{mode} — 粘贴链接后点击解析 | v{APP_VERSION}"
            )
        else:
            self.status_bar.showMessage(
                f"未检测到 ffmpeg，仅显示单文件清晰度 | v{APP_VERSION}"
            )

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_updates)
        self._timer.start(200)

        self._update_bridge = UpdateBridge()
        self._update_bridge.check_done.connect(self._on_check_done)
        self._update_bridge.update_done.connect(self._on_update_done)
        self._update_bridge.progress.connect(self._on_update_progress)
        self._update_worker = UpdateWorker(
            on_check_done=lambda info, err: self._update_bridge.check_done.emit(info, err),
            on_update_done=lambda result: self._update_bridge.update_done.emit(result),
            on_progress=lambda label, pct: self._update_bridge.progress.emit(label, pct),
        )
        self._pending_update_info = None
        self._pending_restart_result: UpdateResult | None = None
        self._updating = False

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(10)

        layout.addWidget(QLabel("视频链接:"))

        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴 YouTube 或其他视频链接...")
        self.url_input.setMinimumHeight(36)
        self.url_input.returnPressed.connect(self._on_parse)
        url_row.addWidget(self.url_input, stretch=1)

        self.parse_btn = QPushButton("解析")
        self.parse_btn.setMinimumHeight(36)
        self.parse_btn.setMinimumWidth(80)
        self.parse_btn.clicked.connect(self._on_parse)
        url_row.addWidget(self.parse_btn)

        self.update_btn = QPushButton("检查更新")
        self.update_btn.setMinimumHeight(36)
        self.update_btn.setMinimumWidth(96)
        self.update_btn.clicked.connect(self._on_check_update)
        url_row.addWidget(self.update_btn)
        layout.addLayout(url_row)

        layout.addWidget(QLabel("下载列表:"))

        self.task_table = QTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(
            ["标题", "状态", "清晰度", "进度", "速度", "剩余时间"]
        )
        for col in range(6):
            self.task_table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Fixed
            )
        self.task_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.task_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.task_table.setAlternatingRowColors(True)
        layout.addWidget(self.task_table, stretch=1)
        self._apply_column_widths()

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _on_parse(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入视频链接")
            return

        self.parse_btn.setEnabled(False)
        self.status_bar.showMessage("正在解析视频信息...")
        self.fetcher.fetch(url)

    def _enqueue_format_result(self, result: FormatFetchResult) -> None:
        self._format_queue.put(result)

    def _set_cell_text(self, row: int, col: int, text: str, *, align_left: bool = False) -> None:
        item = self.task_table.item(row, col)
        if item is None:
            item = QTableWidgetItem(text)
            align = (
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                if align_left
                else Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
            )
            item.setTextAlignment(align)
            self.task_table.setItem(row, col, item)
        else:
            item.setText(text)

    def _apply_column_widths(self) -> None:
        """按比例分配列宽，保证总宽铺满。

        约定：
        - 标题 40%
        - 清晰度 10%
        - 状态 10%
        - 速度 10%
        - 进度 15%
        - 剩余时间 15%
        """
        total = max(600, self.task_table.viewport().width())
        widths = {
            self.COL_TITLE: int(total * 0.40),
            self.COL_STATUS: int(total * 0.10),
            self.COL_QUALITY: int(total * 0.10),
            self.COL_PROGRESS: int(total * 0.15),
            self.COL_SPEED: int(total * 0.10),
        }
        used = sum(widths.values())
        widths[self.COL_ETA] = max(80, total - used)

        for col, w in widths.items():
            self.task_table.setColumnWidth(col, max(80, w))

    def _refresh_table_layout(self) -> None:
        self._apply_column_widths()
        self._refresh_all_title_cells()

    def _set_title_cell(self, row: int, full_title: str) -> None:
        self._row_full_titles[row] = full_title
        available_width = max(50, self.task_table.columnWidth(self.COL_TITLE) - 16)
        elided = self.task_table.fontMetrics().elidedText(
            full_title,
            Qt.TextElideMode.ElideRight,
            available_width,
        )
        self._set_cell_text(row, self.COL_TITLE, elided, align_left=True)
        item = self.task_table.item(row, self.COL_TITLE)
        if item is not None:
            item.setToolTip(full_title)

    def _refresh_all_title_cells(self) -> None:
        for row, full_title in self._row_full_titles.items():
            if row < self.task_table.rowCount():
                self._set_title_cell(row, full_title)

    def _add_pending_row(self, result: FormatFetchResult) -> None:
        task_id = str(uuid.uuid4())[:8]
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)
        self._task_rows[task_id] = row
        self._pending_tasks[task_id] = PendingTask(
            task_id=task_id,
            url=result.url,
            title=result.title,
            formats=result.formats,
        )

        self._set_title_cell(row, result.title)
        self._set_cell_text(row, self.COL_STATUS, "待下载")

        combo = QComboBox()
        for option in result.formats:
            combo.addItem(option.quality, option.format_selector)
        combo.setCurrentIndex(pick_default_quality_index(result.formats))
        self.task_table.setCellWidget(row, self.COL_QUALITY, combo)

        start_btn = QPushButton("开始下载")
        start_btn.clicked.connect(lambda _checked=False, tid=task_id: self._on_start_download(tid))
        self.task_table.setCellWidget(row, self.COL_PROGRESS, start_btn)
        self._refresh_table_layout()

    def _apply_format_result(self, result: FormatFetchResult) -> None:
        self.parse_btn.setEnabled(True)
        if not result.ok:
            self.status_bar.showMessage(f"解析失败: {result.error[:120]}")
            QMessageBox.warning(self, "解析失败", result.error)
            return

        self._add_pending_row(result)
        self.url_input.clear()
        self.status_bar.showMessage(f"已添加: {result.title[:60]} — 请选择清晰度后点击开始下载")

    def _on_start_download(self, task_id: str) -> None:
        pending = self._pending_tasks.get(task_id)
        if pending is None:
            return

        row = self._task_rows[task_id]
        combo = self.task_table.cellWidget(row, self.COL_QUALITY)
        if not isinstance(combo, QComboBox):
            return

        format_selector = combo.currentData()
        quality_label_text = combo.currentText()

        if format_needs_merge(str(format_selector)) and not has_ffmpeg():
            QMessageBox.warning(
                self,
                "需要 ffmpeg",
                "所选清晰度需要合并音视频，但系统未安装 ffmpeg。\n\n"
                "请安装后重试：\n  macOS: brew install ffmpeg",
            )
            return

        output_dir = QFileDialog.getExistingDirectory(
            self,
            "选择保存目录",
            self._last_download_dir,
        )
        if not output_dir:
            return

        self._last_download_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.task_table.removeCellWidget(row, self.COL_QUALITY)
        self.task_table.removeCellWidget(row, self.COL_PROGRESS)
        self._set_cell_text(row, self.COL_QUALITY, quality_label_text)
        self._set_cell_text(row, self.COL_STATUS, "准备中")
        self._set_cell_text(row, self.COL_PROGRESS, "0%")

        del self._pending_tasks[task_id]

        self.manager.start_download(
            pending.url,
            output_dir,
            str(format_selector),
            pending.title,
            task_id=task_id,
        )
        self.status_bar.showMessage(f"已开始下载: {pending.title[:80]}")

    def _enqueue_update(self, task: DownloadTask) -> None:
        self._ui_queue.put(task)

    def _poll_updates(self) -> None:
        while True:
            try:
                result = self._format_queue.get_nowait()
            except queue.Empty:
                break
            self._apply_format_result(result)

        while True:
            try:
                task = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            self._update_task_row(task)

    def _update_task_row(self, task: DownloadTask) -> None:
        if task.task_id in self._pending_tasks:
            return

        row = self._task_rows.get(task.task_id)
        if row is None:
            return

        self._set_title_cell(row, task.title)
        self._set_cell_text(row, self.COL_STATUS, task.status)
        self._set_cell_text(row, self.COL_PROGRESS, task.progress)
        self._set_cell_text(row, self.COL_SPEED, task.speed)
        self._set_cell_text(row, self.COL_ETA, task.eta)

        if task.status == "完成":
            self.status_bar.showMessage(f"下载完成: {task.title}")
        elif task.status == "失败":
            self.status_bar.showMessage(f"下载失败: {task.error[:120]}")
            self._set_cell_text(row, self.COL_PROGRESS, "失败")

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _on_check_update(self) -> None:
        if self._updating:
            return
        self.update_btn.setEnabled(False)
        self.status_bar.showMessage("正在检查更新...")
        self._update_worker.check_async()

    def _on_check_done(self, info: VersionInfo | None, error: str) -> None:
        self.update_btn.setEnabled(True)
        if error:
            self.status_bar.showMessage(f"检查更新失败: {error[:120]}")
            QMessageBox.warning(self, "检查更新", f"无法检查更新：\n{error}")
            return
        if info is None:
            return

        summary = format_update_summary(info)
        if not info.any_update_available:
            self.status_bar.showMessage(f"已是最新版本 | v{APP_VERSION}")
            QMessageBox.information(self, "检查更新", summary)
            return

        if not is_frozen():
            if info.app_update_available:
                self.status_bar.showMessage("开发模式请下载 Release 安装包更新应用")
                QMessageBox.information(
                    self,
                    "检查更新",
                    summary
                    + "\n\n当前为开发模式，应用本体请下载 Release 安装包；"
                    "yt-dlp 可在此直接更新，完成后需重启。",
                )
            if info.ytdlp_update_available:
                answer = QMessageBox.question(
                    self,
                    "更新 yt-dlp",
                    summary + "\n\n是否下载最新 yt-dlp？\n更新后需要重启应用才能生效。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._pending_update_info = VersionInfo(
                        app_current=info.app_current,
                        app_latest=info.app_latest,
                        ytdlp_current=info.ytdlp_current,
                        ytdlp_latest=info.ytdlp_latest,
                        app_update_available=False,
                        ytdlp_update_available=True,
                    )
                    self._start_update()
            elif not info.app_update_available:
                self.status_bar.showMessage(f"已是最新版本 | v{APP_VERSION}")
                QMessageBox.information(self, "检查更新", summary)
            return

        answer = QMessageBox.question(
            self,
            "发现更新",
            summary + "\n\n是否立即更新？\n更新完成后需要重启应用才能生效。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.status_bar.showMessage(f"已取消更新 | v{APP_VERSION}")
            return

        self._pending_update_info = info
        self._start_update()

    def _start_update(self) -> None:
        if self._pending_update_info is None:
            return
        self._updating = True
        self.update_btn.setEnabled(False)
        self.parse_btn.setEnabled(False)
        self.status_bar.showMessage("正在下载更新...")
        self._update_worker.update_async(self._pending_update_info)

    def _on_update_progress(self, label: str, percent: float | None) -> None:
        if percent is None:
            self.status_bar.showMessage(label)
        else:
            self.status_bar.showMessage(f"{label} {percent:.1f}%")

    def _on_update_done(self, result: UpdateResult) -> None:
        self._updating = False
        self.update_btn.setEnabled(True)
        self.parse_btn.setEnabled(True)

        if not result.ok:
            self.status_bar.showMessage(f"更新失败: {result.message[:120]}")
            QMessageBox.warning(self, "更新失败", result.message)
            return

        if not result.needs_restart:
            self.status_bar.showMessage(result.message)
            if result.message != "当前已是最新版本":
                QMessageBox.information(self, "检查更新", result.message)
            return

        self._pending_restart_result = result
        self.status_bar.showMessage(result.message)
        answer = QMessageBox.question(
            self,
            "需要重启",
            result.message + "\n\n是否立即重启应用？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._restart_for_update()
            return

        self.status_bar.showMessage("更新已就绪，请稍后手动重启应用以生效")
        QMessageBox.information(
            self,
            "需要重启",
            "更新文件已准备完成。\n请退出并重新打开应用，新版本才会生效。",
        )

    def _restart_for_update(self) -> None:
        result = self._pending_restart_result
        if result is None:
            return

        self.status_bar.showMessage("正在重启应用...")
        try:
            apply_pending_restart(result, wait_pid=os.getpid())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "重启失败", str(exc))
            return

        QApplication.instance().quit()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_table_layout()


def main() -> None:
    setup_app_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
