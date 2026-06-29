"""检查更新、下载与通过 updater 替换应用 / 热更新 yt-dlp。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bootstrap import user_data_dir, ytdlp_runtime_dir
from version import APP_VERSION, GITHUB_REPO, YTDLP_REPO

ProgressCallback = Callable[[str, float | None], None]

USER_AGENT = f"VideoDownload/{APP_VERSION}"
CHECK_CACHE_TTL = 3600  # 秒，避免频繁请求
_check_cache: tuple[float, VersionInfo] | None = None
_check_cache_lock = threading.Lock()
YTDLP_VERSION_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")
RELEASE_TAG_RE = re.compile(r"/releases/tag/v?([^/?#]+)", re.I)
APP_ASSET_NAMES = {
    "win32": "VideoDownload-Windows.zip",
    "darwin": "VideoDownload.app.zip",
}
WINDOWS_APP_EXE = "VideoDownload.exe"
UPDATER_NAMES = {
    "win32": "VideoDownloadUpdater.exe",
    "darwin": "VideoDownloadUpdater",
}


@dataclass
class UpdateResult:
    ok: bool
    message: str
    needs_restart: bool = False
    app_package: Path | None = None

    @staticmethod
    def failure(message: str) -> "UpdateResult":
        return UpdateResult(ok=False, message=message)


@dataclass
class VersionInfo:
    app_current: str
    app_latest: str
    ytdlp_current: str
    ytdlp_latest: str
    app_update_available: bool
    ytdlp_update_available: bool

    @property
    def any_update_available(self) -> bool:
        return self.app_update_available or self.ytdlp_update_available


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def parse_app_version(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lstrip("v")
    parts: list[int] = []
    for part in cleaned.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts) or (0,)


def parse_ytdlp_version(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def compare_versions(current: str, latest: str, *, kind: str) -> bool:
    parser = parse_app_version if kind == "app" else parse_ytdlp_version
    return parser(latest) > parser(current)


def app_install_dir() -> Path:
    """应用安装目录（Windows 为含 exe 的文件夹）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_executable_path() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve()
    return Path(__file__).resolve().parent / "main.py"


def app_bundle_path() -> Path:
    exe = app_executable_path()
    if sys.platform == "darwin" and exe.suffix != ".py":
        # .../VideoDownload.app/Contents/MacOS/VideoDownload
        return exe.parent.parent.parent
    return app_install_dir()


def updater_executable_path() -> Path:
    name = UPDATER_NAMES.get(sys.platform)
    if not name:
        return Path()
    if is_frozen():
        return app_install_dir() / name
    for candidate in (
        Path(__file__).resolve().parent / "dist" / "VideoDownload" / name,
        Path(__file__).resolve().parent / "dist" / name,
    ):
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parent / "dist" / "VideoDownload" / name


def get_current_ytdlp_version() -> str:
    import yt_dlp

    return yt_dlp.version.__version__


def _load_check_cache() -> VersionInfo | None:
    global _check_cache
    with _check_cache_lock:
        if _check_cache is None:
            return None
        cached_at, info = _check_cache
        if time.time() - cached_at > CHECK_CACHE_TTL:
            _check_cache = None
            return None
        return info


def _save_check_cache(info: VersionInfo) -> None:
    global _check_cache
    with _check_cache_lock:
        _check_cache = (time.time(), info)


def _request_headers(*, accept: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _normalize_http_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403 and "rate limit" in str(exc.reason).lower():
            return (
                "GitHub 请求过于频繁（API 限流）。\n"
                "请稍后再试，或到 GitHub Release 页面手动下载。"
            )
        if exc.code == 404:
            return "未找到发布版本，请确认仓库已创建 Release。"
        return f"HTTP {exc.code}: {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"网络错误: {exc.reason}"
    return str(exc)


def _github_request(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers=_request_headers(accept="application/vnd.github+json"),
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers=_request_headers())
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def _fetch_final_url(url: str) -> str:
    """跟随重定向，不消耗 GitHub API 配额。"""
    request = urllib.request.Request(url, headers=_request_headers())
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.geturl()


def _parse_release_tag(url: str) -> str | None:
    match = RELEASE_TAG_RE.search(url)
    if not match:
        return None
    return match.group(1).lstrip("v")


def fetch_latest_app_version() -> str:
    # 优先走 Release 页面重定向，避免 api.github.com 限流
    final_url = _fetch_final_url(f"https://github.com/{GITHUB_REPO}/releases/latest")
    tag = _parse_release_tag(final_url)
    if tag:
        return tag

    # 备用：GitHub API（可配合环境变量 GITHUB_TOKEN 提高配额）
    data = _github_request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
    tag = str(data.get("tag_name", "")).lstrip("v")
    if not tag:
        raise RuntimeError("无法获取应用最新版本")
    return tag


def latest_app_download_url() -> str:
    asset_name = APP_ASSET_NAMES.get(sys.platform)
    if not asset_name:
        raise RuntimeError("当前平台暂不支持自动更新应用")
    return f"https://github.com/{GITHUB_REPO}/releases/latest/download/{asset_name}"


def fetch_latest_ytdlp_version() -> str:
    text = _fetch_text(
        f"https://raw.githubusercontent.com/{YTDLP_REPO}/master/yt_dlp/version.py"
    )
    match = YTDLP_VERSION_RE.search(text)
    if not match:
        raise RuntimeError("无法解析 yt-dlp 最新版本")
    return match.group(1)


def check_for_updates(*, use_cache: bool = True) -> VersionInfo:
    if use_cache:
        cached = _load_check_cache()
        if cached is not None:
            return cached

    app_latest = fetch_latest_app_version()
    ytdlp_latest = fetch_latest_ytdlp_version()
    ytdlp_current = get_current_ytdlp_version()
    info = VersionInfo(
        app_current=APP_VERSION,
        app_latest=app_latest,
        ytdlp_current=ytdlp_current,
        ytdlp_latest=ytdlp_latest,
        app_update_available=compare_versions(APP_VERSION, app_latest, kind="app"),
        ytdlp_update_available=compare_versions(
            ytdlp_current, ytdlp_latest, kind="ytdlp"
        ),
    )
    _save_check_cache(info)
    return info


def _download_file(
    url: str,
    dest: Path,
    *,
    on_progress: ProgressCallback | None = None,
    label: str = "下载中",
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers=_request_headers())
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk_size = 256 * 1024
        with dest.open("wb") as handle:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if on_progress is not None:
                    percent = (downloaded / total * 100) if total else None
                    on_progress(label, percent)


def download_app_package(dest: Path, *, on_progress: ProgressCallback | None = None) -> Path:
    url = latest_app_download_url()
    _download_file(url, dest, on_progress=on_progress, label="下载应用更新")
    return dest


def download_ytdlp_package(dest: Path, *, on_progress: ProgressCallback | None = None) -> Path:
    url = f"https://github.com/{YTDLP_REPO}/archive/master.tar.gz"
    _download_file(url, dest, on_progress=on_progress, label="下载 yt-dlp 更新")
    return dest


def install_ytdlp_package(tarball: Path, *, new_version: str) -> None:
    runtime = ytdlp_runtime_dir()
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extractall(tmp_path)
        extracted_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
        if not extracted_dirs:
            raise RuntimeError("yt-dlp 压缩包内容无效")
        source_dir = extracted_dirs[0]
        if runtime.exists():
            shutil.rmtree(runtime)
        shutil.copytree(source_dir, runtime)
    (user_data_dir() / "yt_dlp" / "version.txt").write_text(new_version, encoding="utf-8")


def _spawn_updater(args: list[str]) -> None:
    updater = updater_executable_path()
    if not updater.is_file():
        raise RuntimeError(f"未找到更新器: {updater.name}")

    if sys.platform == "win32":
        staged = user_data_dir() / "VideoDownloadUpdater.exe"
        shutil.copy2(updater, staged)
        command = [str(staged), *args]
        workdir = os.environ.get("TEMP") or os.environ.get("TMP") or str(staged.parent)
    else:
        command = [str(updater), *args]
        workdir = str(updater.parent)

    if sys.platform == "win32":
        subprocess.Popen(
            command,
            cwd=workdir,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(command, cwd=workdir, start_new_session=True)


def replace_app_via_updater(source: Path, *, wait_pid: int, restart: bool = True) -> None:
    if sys.platform == "win32":
        target = app_install_dir()
    elif sys.platform == "darwin":
        target = app_bundle_path()
    else:
        raise RuntimeError("当前平台暂不支持自动替换应用")

    args = [
        "--source",
        str(source),
        "--target",
        str(target),
        "--wait-pid",
        str(wait_pid),
    ]
    if restart:
        args.append("--restart")
    _spawn_updater(args)


def restart_app_via_updater(*, wait_pid: int) -> None:
    if sys.platform == "win32":
        target = app_executable_path()
    elif sys.platform == "darwin":
        target = app_bundle_path()
    else:
        raise RuntimeError("当前平台暂不支持自动重启")

    _spawn_updater(
        [
            "--target",
            str(target),
            "--wait-pid",
            str(wait_pid),
            "--restart-only",
        ]
    )


def restart_application(*, wait_pid: int | None = None) -> None:
    """请求重启应用；打包版通过 updater，开发模式重新拉起 main.py。"""
    pid = wait_pid if wait_pid is not None else os.getpid()
    if is_frozen():
        restart_app_via_updater(wait_pid=pid)
        return

    main_script = Path(__file__).resolve().parent / "main.py"
    subprocess.Popen(
        [sys.executable, str(main_script)],
        cwd=str(main_script.parent),
        start_new_session=True,
    )


def apply_pending_restart(
    result: UpdateResult,
    *,
    wait_pid: int,
) -> None:
    """在用户确认重启后，替换应用包或仅重启进程。"""
    if result.app_package is not None:
        replace_app_via_updater(result.app_package, wait_pid=wait_pid, restart=True)
        return
    restart_application(wait_pid=wait_pid)


def perform_update(
    info: VersionInfo,
    *,
    on_progress: ProgressCallback | None = None,
) -> UpdateResult:
    download_dir = user_data_dir() / "updates"
    download_dir.mkdir(parents=True, exist_ok=True)
    updated = False
    app_package: Path | None = None

    if info.app_update_available:
        if not is_frozen():
            return UpdateResult.failure("开发模式请下载 Release 安装包更新应用")
        if sys.platform == "win32":
            app_package = download_dir / "VideoDownload-Windows.zip"
        else:
            app_package = download_dir / "VideoDownload.app.zip"
        download_app_package(app_package, on_progress=on_progress)
        updated = True

    elif info.ytdlp_update_available:
        tarball = download_dir / "yt-dlp-master.tar.gz"
        download_ytdlp_package(tarball, on_progress=on_progress)
        install_ytdlp_package(tarball, new_version=info.ytdlp_latest)
        updated = True

    if not updated:
        return UpdateResult(ok=True, message="当前已是最新版本")

    if app_package is not None:
        message = "应用更新已下载，重启后将自动替换并生效。"
    else:
        message = f"yt-dlp 已更新至 {info.ytdlp_latest}，重启后生效。"

    return UpdateResult(
        ok=True,
        message=message,
        needs_restart=True,
        app_package=app_package,
    )


def format_update_summary(info: VersionInfo) -> str:
    lines = [
        f"应用版本：{info.app_current}",
        f"应用最新：{info.app_latest}"
        + ("（有更新）" if info.app_update_available else "（已最新）"),
        "",
        f"yt-dlp：{info.ytdlp_current}",
        f"yt-dlp 最新：{info.ytdlp_latest}"
        + ("（有更新）" if info.ytdlp_update_available else "（已最新）"),
    ]
    if info.app_update_available and info.ytdlp_update_available:
        lines.extend(["", "将下载最新应用包（内含新版 yt-dlp），完成后需重启。"])
    elif info.app_update_available:
        lines.extend(["", "将下载最新应用，完成后需重启。"])
    elif info.ytdlp_update_available:
        lines.extend(["", "将下载最新 yt-dlp，完成后需重启应用。"])
    else:
        lines.extend(["", "当前已是最新版本。"])
    return "\n".join(lines)


class UpdateWorker:
    def __init__(
        self,
        *,
        on_check_done: Callable[[VersionInfo | None, str], None],
        on_update_done: Callable[[UpdateResult], None],
        on_progress: ProgressCallback,
    ) -> None:
        self._on_check_done = on_check_done
        self._on_update_done = on_update_done
        self._on_progress = on_progress

    def check_async(self) -> None:
        threading.Thread(target=self._check, daemon=True).start()

    def update_async(self, info: VersionInfo) -> None:
        threading.Thread(
            target=self._update,
            args=(info,),
            daemon=True,
        ).start()

    def _check(self) -> None:
        try:
            info = check_for_updates()
            self._on_check_done(info, "")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            self._on_check_done(None, _normalize_http_error(exc))

    def _update(self, info: VersionInfo) -> None:
        try:
            result = perform_update(info, on_progress=self._on_progress)
            self._on_update_done(result)
        except Exception as exc:  # noqa: BLE001
            self._on_update_done(UpdateResult.failure(str(exc)))
