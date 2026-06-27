#!/usr/bin/env python3
"""独立更新器：等待主程序退出后替换文件并可选重启。"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

WINDOWS_APP_EXE = "VideoDownload.exe"


def wait_for_pid(pid: int, timeout: float = 120.0) -> bool:
    if pid <= 0:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.3)
    return False


def remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def replace_file(source: Path, target: Path) -> None:
    backup = target.with_name(target.name + ".old")
    remove_path(backup)
    if target.exists():
        target.rename(backup)
    shutil.move(str(source), str(target))
    remove_path(backup)


@contextmanager
def extract_zip(zip_path: Path):
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp_dir)
        yield Path(tmp_dir)


@contextmanager
def extract_mac_app(zip_path: Path):
    with extract_zip(zip_path) as tmp_path:
        apps = list(tmp_path.rglob("*.app"))
        if not apps:
            raise RuntimeError("压缩包中未找到 .app")
        yield apps[0]


def find_windows_package_root(tmp_path: Path) -> Path:
    matches = list(tmp_path.rglob(WINDOWS_APP_EXE))
    if not matches:
        raise RuntimeError(f"压缩包中未找到 {WINDOWS_APP_EXE}")
    return matches[0].parent


def replace_windows_folder(source_zip: Path, target_dir: Path) -> None:
    with extract_zip(source_zip) as tmp_path:
        source_folder = find_windows_package_root(tmp_path)
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in source_folder.iterdir():
            dest = target_dir / item.name
            if item.is_dir():
                remove_path(dest)
                shutil.copytree(item, dest)
            else:
                if dest.exists():
                    dest.unlink()
                shutil.copy2(item, dest)


def replace_mac_app(source_zip: Path, target_app: Path) -> None:
    backup = target_app.with_name(target_app.name + ".old")
    remove_path(backup)
    with extract_mac_app(source_zip) as extracted_app:
        if target_app.exists():
            target_app.rename(backup)
        shutil.move(str(extracted_app), str(target_app))
    remove_path(backup)


def launch_target(target: Path) -> None:
    if target.is_dir():
        exe = target / WINDOWS_APP_EXE
        if not exe.is_file():
            raise RuntimeError(f"目录中未找到 {WINDOWS_APP_EXE}: {target}")
        subprocess.Popen([str(exe)], cwd=str(target), close_fds=True)
        return
    if sys.platform == "darwin" and target.suffix == ".app":
        subprocess.Popen(["open", "-n", str(target)], close_fds=True)
        return
    subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="VideoDownload updater")
    parser.add_argument("--source", help="新文件或压缩包路径")
    parser.add_argument("--target", required=True, help="待替换目标路径（exe、目录或 .app）")
    parser.add_argument("--wait-pid", type=int, default=0, help="等待退出的进程 PID")
    parser.add_argument("--restart", action="store_true", help="替换后启动目标")
    parser.add_argument(
        "--restart-only",
        action="store_true",
        help="仅等待并重启目标，不替换文件",
    )
    args = parser.parse_args()

    target = Path(args.target)
    if not wait_for_pid(args.wait_pid):
        print("等待主程序退出超时", file=sys.stderr)
        return 1

    if not args.restart_only:
        if not args.source:
            print("缺少 --source", file=sys.stderr)
            return 1
        source = Path(args.source)
        if not source.is_file():
            print(f"更新文件不存在: {source}", file=sys.stderr)
            return 1

        if sys.platform == "win32" and source.suffix == ".zip":
            replace_windows_folder(source, target)
        elif sys.platform == "darwin" and target.suffix == ".app":
            replace_mac_app(source, target)
        else:
            replace_file(source, target)

    if args.restart or args.restart_only:
        launch_target(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
