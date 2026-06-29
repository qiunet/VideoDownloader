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


def is_zip_package(path: Path) -> bool:
    return path.is_file() and zipfile.is_zipfile(path)


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
    if target.is_dir():
        raise RuntimeError(
            f"不支持将单个文件替换到目录: {target}。"
            " Windows 应用更新应使用 zip 包。"
        )
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


def copy_item_with_retry(source: Path, dest: Path, *, retries: int = 20) -> None:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    backup = dest.with_name(dest.name + ".old")
                    remove_path(backup)
                    dest.rename(backup)
            if source.is_dir():
                shutil.copytree(source, dest)
            else:
                shutil.copy2(source, dest)
            remove_path(dest.with_name(dest.name + ".old"))
            return
        except (PermissionError, OSError) as exc:
            last_error = exc
            time.sleep(0.5 + attempt * 0.1)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"无法写入: {dest}")


def replace_windows_folder(source_zip: Path, target_dir: Path) -> None:
    running_exe = Path(sys.executable).resolve()
    with extract_zip(source_zip) as tmp_path:
        source_folder = find_windows_package_root(tmp_path)
        target_dir.mkdir(parents=True, exist_ok=True)
        for item in source_folder.iterdir():
            dest = target_dir / item.name
            if item.is_file():
                try:
                    if dest.resolve() == running_exe:
                        _log(f"跳过正在运行的更新器: {dest}")
                        continue
                except OSError:
                    pass
            copy_item_with_retry(item, dest)


def replace_mac_app(source_zip: Path, target_app: Path) -> None:
    backup = target_app.with_name(target_app.name + ".old")
    remove_path(backup)
    with extract_mac_app(source_zip) as extracted_app:
        if target_app.exists():
            target_app.rename(backup)
        shutil.move(str(extracted_app), str(target_app))
    remove_path(backup)


def _log(message: str) -> None:
    print(message, flush=True)
    try:
        from bootstrap import user_data_dir

        log_dir = user_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "updater-latest.log"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def launch_target(target: Path) -> None:
    if target.is_dir():
        exe = target / WINDOWS_APP_EXE
        if not exe.is_file():
            raise RuntimeError(f"目录中未找到 {WINDOWS_APP_EXE}: {target}")
    elif target.suffix.lower() == ".exe":
        exe = target
    elif sys.platform == "darwin" and target.suffix == ".app":
        _log(f"启动 macOS 应用: {target}")
        subprocess.Popen(["open", "-n", str(target)], close_fds=True)
        return
    else:
        raise RuntimeError(f"无法识别的启动目标: {target}")

    _log(f"启动应用: {exe}")
    if sys.platform == "win32":
        subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return
    subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True, close_fds=True)


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

    _log(f"updater 启动 argv={' '.join(sys.argv[1:])}")

    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001
        _log(f"updater 失败: {exc}")
        raise


def _run(args: argparse.Namespace) -> int:
    target = Path(args.target)
    if not wait_for_pid(args.wait_pid):
        _log(f"等待主程序退出超时 pid={args.wait_pid}")
        print("等待主程序退出超时", file=sys.stderr)
        return 1

    if sys.platform == "win32" and args.wait_pid > 0:
        # 主进程退出后，Windows 仍可能短暂占用 exe/工作目录
        time.sleep(1.5)

    if not args.restart_only:
        if not args.source:
            print("缺少 --source", file=sys.stderr)
            return 1
        source = Path(args.source)
        if not source.is_file():
            _log(f"更新文件不存在: {source}")
            print(f"更新文件不存在: {source}", file=sys.stderr)
            return 1

        _log(f"开始替换 target={target} source={source}")
        if sys.platform == "win32" and is_zip_package(source):
            replace_windows_folder(source, target)
        elif sys.platform == "darwin" and target.suffix == ".app" and is_zip_package(source):
            replace_mac_app(source, target)
        else:
            replace_file(source, target)
        _log("文件替换完成")

    if args.restart or args.restart_only:
        launch_target(target)
        _log("已请求启动应用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
