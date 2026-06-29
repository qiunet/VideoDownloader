"""抖音链接适配器：基于 f2 库解析与下载，供 GUI 在抖音链接时替代 yt-dlp。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from app_log import log_file_hint, setup_app_logging

DOUYIN_FORMAT_PREFIX = "douyin:"
DEFAULT_COOKIE_BROWSER = "chrome"
COOKIE_TTL_SECONDS = 300

_COOKIE_CACHE: tuple[float, str] | None = None

logger = logging.getLogger("douyin_adapter")

_BROWSER_USER_DATA: dict[str, list[str]] = {
    "chrome": ["Google/Chrome/User Data"],
    "edge": ["Microsoft/Edge/User Data"],
    "chromium": ["Chromium/User Data"],
    "brave": ["BraveSoftware/Brave-Browser/User Data"],
}


@dataclass(frozen=True)
class DouyinFormat:
    format_selector: str
    quality: str


@dataclass
class DouyinProbeResult:
    title: str
    formats: list[DouyinFormat]
    aweme_id: str


def _load_f2() -> dict[str, Any]:
    """延迟加载 f2，避免启动时读取配置；打包版需包含 f2/conf 资源。"""
    setup_app_logging()
    import f2
    from f2.apps.douyin.handler import DouyinHandler
    from f2.apps.douyin.utils import AwemeIdFetcher, ClientConfManager
    from f2.exceptions.api_exceptions import APIResponseError
    from f2.utils.conf_manager import ConfigManager
    from f2.utils.utils import get_cookie_from_browser, merge_config, split_dict_cookie

    return {
        "f2": f2,
        "DouyinHandler": DouyinHandler,
        "AwemeIdFetcher": AwemeIdFetcher,
        "ClientConfManager": ClientConfManager,
        "APIResponseError": APIResponseError,
        "ConfigManager": ConfigManager,
        "get_cookie_from_browser": get_cookie_from_browser,
        "merge_config": merge_config,
        "split_dict_cookie": split_dict_cookie,
    }


def is_douyin_url(url: str) -> bool:
    host = urlparse(url.strip()).netloc.lower().removeprefix("www.")
    if not host:
        return False
    return host.endswith("douyin.com") or host == "douyin.com"


def is_douyin_format(format_selector: str) -> bool:
    return str(format_selector).startswith(DOUYIN_FORMAT_PREFIX)


def _quality_label(height: int) -> str:
    if height >= 2160:
        return "4K"
    if height <= 0:
        return "未知"
    return f"{height}p"


def _safe_filename(name: str, *, max_len: int = 180) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name).strip(" .")
    return (cleaned[:max_len] if cleaned else "douyin_video")


def _extract_aweme_id_from_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id"):
        values = query.get(key)
        if values and values[0].isdigit():
            return values[0]

    match = re.search(r"/(?:video|note)/(\d+)", parsed.path)
    if match:
        return match.group(1)
    return None


async def _resolve_aweme_id(url: str) -> str:
    aweme_id = _extract_aweme_id_from_url(url)
    if aweme_id:
        return aweme_id
    f2 = _load_f2()
    return await f2["AwemeIdFetcher"].get_aweme_id(url)


def _browser_candidates() -> list[str]:
    if sys.platform == "win32":
        return ["chrome", "edge", "chromium", "brave", "firefox"]
    if sys.platform == "darwin":
        return ["chrome", "safari", "edge", "chromium", "brave", "firefox"]
    return ["chrome", "chromium", "edge", "brave", "firefox"]


def _firefox_profile_roots() -> list[Path]:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return [Path(appdata) / "Mozilla/Firefox/Profiles"] if appdata else []
    if sys.platform == "darwin":
        return [Path.home() / "Library/Application Support/Firefox/Profiles"]
    return [Path.home() / ".mozilla/firefox"]


def _iter_browser_cookie_files(browser: str) -> list[Path]:
    cookie_files: list[Path] = []

    if browser == "firefox":
        for profiles_root in _firefox_profile_roots():
            if not profiles_root.is_dir():
                continue
            for profile_dir in sorted(profiles_root.iterdir()):
                if not profile_dir.is_dir():
                    continue
                cookie_db = profile_dir / "cookies.sqlite"
                if cookie_db.is_file():
                    cookie_files.append(cookie_db)
        return cookie_files

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return cookie_files

    rel_paths = _BROWSER_USER_DATA.get(browser, [])
    for rel in rel_paths:
        user_data = Path(local_app_data) / rel
        if not user_data.is_dir():
            continue
        for profile_dir in sorted(user_data.iterdir()):
            if not profile_dir.is_dir():
                continue
            for name in ("Network/Cookies", "Cookies"):
                cookie_file = profile_dir / name
                if cookie_file.is_file():
                    cookie_files.append(cookie_file)
    return cookie_files


def _domain_matches(cookie_domain: str, domain: str) -> bool:
    left = cookie_domain.lstrip(".").lower()
    right = domain.lstrip(".").lower()
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def _cookies_from_jar(jar: Any, domain: str) -> dict[str, str]:
    return {
        cookie.name: cookie.value
        for cookie in jar
        if _domain_matches(cookie.domain, domain)
    }


def _read_browser_cookies(browser: str, domain: str) -> dict[str, str]:
    import browser_cookie3

    readers = {
        "chrome": browser_cookie3.chrome,
        "edge": browser_cookie3.edge,
        "chromium": browser_cookie3.chromium,
        "brave": browser_cookie3.brave,
        "firefox": browser_cookie3.firefox,
        "safari": browser_cookie3.safari,
        "opera": browser_cookie3.opera,
        "vivaldi": browser_cookie3.vivaldi,
    }
    reader = readers.get(browser)
    if reader is None:
        logger.warning("不支持的浏览器: %s", browser)
        return {}

    try:
        cookies = _cookies_from_jar(reader(domain_name=domain), domain)
        if cookies:
            logger.info("从 %s 默认配置读取到 %d 个 %s Cookie", browser, len(cookies), domain)
            return cookies
        logger.warning("%s 默认配置中未找到 %s Cookie", browser, domain)
    except PermissionError:
        logger.exception("读取 %s Cookie 权限不足（请完全退出该浏览器）", browser)
    except Exception:
        logger.exception("读取 %s 默认 Cookie 失败", browser)

    for cookie_file in _iter_browser_cookie_files(browser):
        try:
            cookies = _cookies_from_jar(
                reader(cookie_file=str(cookie_file), domain_name=domain),
                domain,
            )
            if cookies:
                profile = (
                    cookie_file.parent.name
                    if browser == "firefox"
                    else cookie_file.parent.parent.name
                )
                logger.info(
                    "从 %s/%s 读取到 %d 个 %s Cookie",
                    browser,
                    profile,
                    len(cookies),
                    domain,
                )
                return cookies
        except PermissionError:
            logger.exception("读取 %s Cookie 文件被占用: %s", browser, cookie_file)
        except Exception:
            logger.exception("读取 %s Cookie 文件失败: %s", browser, cookie_file)

    return {}


def _cookie_error_message(errors: list[str]) -> str:
    lines = [
        "无法读取浏览器 Cookie，抖音解析需要先在浏览器中打开 douyin.com。",
    ]
    if sys.platform == "win32":
        lines.extend(
            [
                "",
                "Windows 建议：",
                "1. 完全退出 Chrome / Edge（任务管理器中确认无 chrome.exe / msedge.exe）",
                "2. 先在浏览器访问 https://www.douyin.com 后再重试",
                "3. 若 Chrome / Edge 仍失败，可在 Firefox 打开 douyin.com 后重试（Firefox 排在最后尝试）",
            ]
        )
    else:
        lines.append("请完全退出对应浏览器后重试。")

    if errors:
        lines.extend(["", "尝试记录：", *errors[:6]])
    lines.append(log_file_hint())
    return "\n".join(lines)


def _get_cookie(browser: str = DEFAULT_COOKIE_BROWSER) -> str:
    global _COOKIE_CACHE
    now = time.time()
    if _COOKIE_CACHE and now - _COOKIE_CACHE[0] < COOKIE_TTL_SECONDS:
        return _COOKIE_CACHE[1]

    f2 = _load_f2()
    split_dict_cookie = f2["split_dict_cookie"]

    browsers = [browser] if browser else []
    for candidate in _browser_candidates():
        if candidate not in browsers:
            browsers.append(candidate)

    errors: list[str] = []
    for name in browsers:
        raw = _read_browser_cookies(name, "douyin.com")
        if not raw:
            errors.append(f"{name}: 未找到 douyin.com Cookie")
            continue
        cookie = split_dict_cookie(raw)
        if not cookie:
            errors.append(f"{name}: Cookie 为空")
            continue
        _COOKIE_CACHE = (now, cookie)
        return cookie

    logger.error("所有浏览器 Cookie 读取失败: %s", "; ".join(errors))
    raise RuntimeError(_cookie_error_message(errors))


def _build_handler_kwargs(url: str, cookie: str) -> dict:
    f2 = _load_f2()
    main_manager = f2["ConfigManager"](f2["f2"].APP_CONFIG_FILE_PATH)
    main_conf = main_manager.get_config("douyin")
    main_conf["proxies"] = f2["ClientConfManager"].proxies()
    kwargs = f2["merge_config"](main_conf, main_conf, url=url, mode="one", cookie=cookie)
    kwargs.setdefault("headers", {})
    kwargs["headers"]["User-Agent"] = f2["ClientConfManager"].user_agent()
    kwargs["headers"]["Referer"] = f2["ClientConfManager"].referer()
    return kwargs


def _pick_play_url(bit_rate: dict) -> str | None:
    play_addr = bit_rate.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    if url_list:
        return str(url_list[0])
    return None


def _build_formats(raw: dict, aweme_data: dict) -> list[DouyinFormat]:
    detail = raw.get("aweme_detail") or {}
    aweme_type = detail.get("aweme_type")
    if aweme_type == 68:
        raise RuntimeError("该链接为图集作品，当前版本暂不支持下载图集。")

    bit_rates = (detail.get("video") or {}).get("bit_rate") or []
    if not bit_rates:
        music_url = aweme_data.get("music_play_url")
        if music_url:
            return [DouyinFormat(f"{DOUYIN_FORMAT_PREFIX}audio", "仅音频")]
        raise RuntimeError("未找到可下载的视频流。")

    best_by_height: dict[int, tuple[int, int]] = {}
    for index, item in enumerate(bit_rates):
        if not isinstance(item, dict):
            continue
        play_addr = item.get("play_addr") or {}
        height = int(play_addr.get("height") or 0)
        bitrate = int(item.get("bit_rate") or 0)
        if not _pick_play_url(item):
            continue
        current = best_by_height.get(height)
        if current is None or bitrate > current[1]:
            best_by_height[height] = (index, bitrate)

    if not best_by_height:
        return [DouyinFormat(f"{DOUYIN_FORMAT_PREFIX}idx:0", "最佳")]

    formats: list[DouyinFormat] = [
        DouyinFormat(f"{DOUYIN_FORMAT_PREFIX}idx:0", "最佳"),
    ]
    seen_qualities: set[str] = {"最佳"}
    for height in sorted(best_by_height.keys(), reverse=True):
        index = best_by_height[height][0]
        if index == 0:
            continue
        quality = _quality_label(height)
        if quality in seen_qualities:
            quality = f"{quality} ({index})"
        seen_qualities.add(quality)
        formats.append(
            DouyinFormat(f"{DOUYIN_FORMAT_PREFIX}idx:{index}", quality)
        )
    return formats


async def _fetch_probe(url: str) -> DouyinProbeResult:
    f2 = _load_f2()
    cookie = _get_cookie()
    aweme_id = await _resolve_aweme_id(url)
    kwargs = _build_handler_kwargs(url, cookie)
    handler = f2["DouyinHandler"](kwargs)
    try:
        video = await handler.fetch_one_video(aweme_id)
    except f2["APIResponseError"] as exc:
        raise RuntimeError(str(exc)) from exc

    aweme_data = video._to_dict()
    raw = video._to_raw()
    title = aweme_data.get("desc") or aweme_data.get("desc_raw") or f"douyin_{aweme_id}"
    formats = _build_formats(raw, aweme_data)
    return DouyinProbeResult(title=str(title), formats=formats, aweme_id=str(aweme_id))


async def _fetch_download_target(
    url: str,
    format_selector: str,
) -> tuple[str, str, dict]:
    f2 = _load_f2()
    cookie = _get_cookie()
    aweme_id = await _resolve_aweme_id(url)
    kwargs = _build_handler_kwargs(url, cookie)
    handler = f2["DouyinHandler"](kwargs)
    video = await handler.fetch_one_video(aweme_id)
    aweme_data = video._to_dict()
    raw = video._to_raw()
    title = _safe_filename(str(aweme_data.get("desc") or aweme_data.get("desc_raw") or aweme_id))

    if format_selector == f"{DOUYIN_FORMAT_PREFIX}audio":
        music_url = aweme_data.get("music_play_url")
        if not music_url:
            raise RuntimeError("该作品没有可下载的音频。")
        return title, str(music_url), kwargs["headers"] | {"Cookie": cookie}

    bit_rates = (raw.get("aweme_detail") or {}).get("video", {}).get("bit_rate") or []
    if not bit_rates:
        raise RuntimeError("未找到可下载的视频流。")

    if format_selector == f"{DOUYIN_FORMAT_PREFIX}idx:0":
        index = 0
    else:
        match = re.fullmatch(rf"{re.escape(DOUYIN_FORMAT_PREFIX)}idx:(\d+)", format_selector)
        if not match:
            raise RuntimeError(f"未知的抖音清晰度选项：{format_selector}")
        index = int(match.group(1))

    if index >= len(bit_rates):
        raise RuntimeError("所选清晰度不可用，请重新解析后重试。")

    play_url = _pick_play_url(bit_rates[index])
    if not play_url:
        raise RuntimeError("无法获取视频下载地址。")

    headers = dict(kwargs.get("headers") or {})
    headers["Cookie"] = cookie
    return title, play_url, headers


def _download_stream(
    url: str,
    dest: Path,
    headers: dict,
    progress_hook: Callable[[dict], None],
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_tick = time.monotonic()
    last_downloaded = 0

    with httpx.Client(follow_redirects=True, timeout=120.0, headers=headers) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0) or None
            downloaded = 0
            progress_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 0,
                    "total_bytes": total,
                    "speed": 0,
                }
            )
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    speed = None
                    if now - last_tick >= 0.4:
                        speed = (downloaded - last_downloaded) / max(now - last_tick, 0.001)
                        last_tick = now
                        last_downloaded = downloaded
                    progress_hook(
                        {
                            "status": "downloading",
                            "downloaded_bytes": downloaded,
                            "total_bytes": total,
                            "speed": speed,
                        }
                    )
    progress_hook({"status": "finished"})


async def _download_async(
    url: str,
    output_dir: str,
    format_selector: str,
    progress_hook: Callable[[dict], None],
) -> Path:
    title, media_url, headers = await _fetch_download_target(url, format_selector)
    ext = ".mp3" if format_selector.endswith("audio") else ".mp4"
    dest = Path(output_dir) / f"{title}{ext}"
    if dest.exists():
        stem = dest.stem
        counter = 1
        while dest.exists():
            dest = Path(output_dir) / f"{stem}_{counter}{ext}"
            counter += 1
    await asyncio.to_thread(_download_stream, media_url, dest, headers, progress_hook)
    return dest


def probe_url(url: str) -> DouyinProbeResult:
    """解析抖音链接，返回标题与清晰度列表。"""
    return asyncio.run(_fetch_probe(url))


def download(
    url: str,
    output_dir: str,
    format_selector: str,
    progress_hook: Callable[[dict], None],
) -> Path:
    """下载抖音作品到指定目录。"""
    return asyncio.run(_download_async(url, output_dir, format_selector, progress_hook))
