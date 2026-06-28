"""抖音链接适配器：基于 f2 库解析与下载，供 GUI 在抖音链接时替代 yt-dlp。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx

import f2
from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import AwemeIdFetcher, ClientConfManager
from f2.exceptions.api_exceptions import APIResponseError
from f2.utils.conf_manager import ConfigManager
from f2.utils.utils import get_cookie_from_browser, merge_config, split_dict_cookie

DOUYIN_FORMAT_PREFIX = "douyin:"
DEFAULT_COOKIE_BROWSER = "chrome"
COOKIE_TTL_SECONDS = 300

_COOKIE_CACHE: tuple[float, str] | None = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DouyinFormat:
    format_selector: str
    quality: str


@dataclass
class DouyinProbeResult:
    title: str
    formats: list[DouyinFormat]
    aweme_id: str


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
    return await AwemeIdFetcher.get_aweme_id(url)


def _get_cookie(browser: str = DEFAULT_COOKIE_BROWSER) -> str:
    global _COOKIE_CACHE
    now = time.time()
    if _COOKIE_CACHE and now - _COOKIE_CACHE[0] < COOKIE_TTL_SECONDS:
        return _COOKIE_CACHE[1]

    try:
        cookie = split_dict_cookie(get_cookie_from_browser(browser, "douyin.com"))
    except PermissionError as exc:
        raise RuntimeError(
            "无法读取浏览器 Cookie，请关闭 Chrome 后重试，或检查系统权限。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"无法从 {browser} 读取 douyin.com Cookie：{exc}"
        ) from exc

    if not cookie:
        raise RuntimeError(
            f"未从 {browser} 获取到 douyin.com Cookie，请先在浏览器中打开抖音。"
        )

    _COOKIE_CACHE = (now, cookie)
    return cookie


def _build_handler_kwargs(url: str, cookie: str) -> dict:
    main_manager = ConfigManager(f2.APP_CONFIG_FILE_PATH)
    main_conf = main_manager.get_config("douyin")
    main_conf["proxies"] = ClientConfManager.proxies()
    kwargs = merge_config(main_conf, main_conf, url=url, mode="one", cookie=cookie)
    kwargs.setdefault("headers", {})
    kwargs["headers"]["User-Agent"] = ClientConfManager.user_agent()
    kwargs["headers"]["Referer"] = ClientConfManager.referer()
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
    cookie = _get_cookie()
    aweme_id = await _resolve_aweme_id(url)
    kwargs = _build_handler_kwargs(url, cookie)
    handler = DouyinHandler(kwargs)
    try:
        video = await handler.fetch_one_video(aweme_id)
    except APIResponseError as exc:
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
    cookie = _get_cookie()
    aweme_id = await _resolve_aweme_id(url)
    kwargs = _build_handler_kwargs(url, cookie)
    handler = DouyinHandler(kwargs)
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
    started = time.monotonic()
    last_tick = started
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
