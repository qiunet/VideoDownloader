#!/usr/bin/env python3
"""Download static ffmpeg builds for bundling into the app."""

from __future__ import annotations

import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"

BTBN_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
EVERMEET_MAC_URL = "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip"


def _download(url: str, dest: Path) -> None:
    print(f"Downloading: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())


def _chmod_exec(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _extract_ffmpeg_from_zip(zip_path: Path, target: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith("/ffmpeg") or n.endswith("/ffmpeg.exe")]
        if not names:
            names = [n for n in zf.namelist() if Path(n).name in {"ffmpeg", "ffmpeg.exe"}]
        if not names:
            raise RuntimeError(f"ffmpeg not found in archive: {zip_path}")

        member = sorted(names, key=len)[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    if target.suffix != ".exe":
        _chmod_exec(target)


def download_macos(target: Path) -> None:
    zip_path = Path(tempfile.mkdtemp()) / "ffmpeg.zip"
    try:
        machine = platform.machine().lower()
        if machine == "arm64":
            url = f"{BTBN_BASE}/ffmpeg-master-latest-macosarm64-gpl.zip"
            try:
                _download(url, zip_path)
                _extract_ffmpeg_from_zip(zip_path, target)
                return
            except Exception as exc:  # noqa: BLE001
                print(f"BtbN arm64 failed, trying evermeet: {exc}")
                zip_path.unlink(missing_ok=True)

        _download(EVERMEET_MAC_URL, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            target.parent.mkdir(parents=True, exist_ok=True)
            zf.extract("ffmpeg", target.parent)
        _chmod_exec(target)
    finally:
        shutil.rmtree(zip_path.parent, ignore_errors=True)


def download_windows(target: Path) -> None:
    zip_path = Path(tempfile.mkdtemp()) / "ffmpeg.zip"
    try:
        url = f"{BTBN_BASE}/ffmpeg-master-latest-win64-gpl.zip"
        _download(url, zip_path)
        _extract_ffmpeg_from_zip(zip_path, target)
    finally:
        shutil.rmtree(zip_path.parent, ignore_errors=True)


def main() -> None:
    system = platform.system()
    if system == "Darwin":
        target = BIN_DIR / "ffmpeg"
    elif system == "Windows":
        target = BIN_DIR / "ffmpeg.exe"
    else:
        print(f"Unsupported platform {system}, place ffmpeg manually in {BIN_DIR}/")
        sys.exit(1)

    if target.is_file():
        print(f"Already exists: {target}")
        return

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    print(f"==> Downloading ffmpeg to {target}")

    if system == "Darwin":
        download_macos(target)
    else:
        download_windows(target)

    if not target.is_file():
        raise SystemExit("Failed to download ffmpeg")

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"Done: {target} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
