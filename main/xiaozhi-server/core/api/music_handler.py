import asyncio
import hashlib
import os
import shutil
import uuid
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from core.providers.music.netease_openapi import NeteaseOpenApiProvider


DEFAULT_MUSIC_EXTENSIONS = (".mp3", ".wav", ".p3", ".ogg", ".opus")
_conversion_locks = {}
_remote_tracks = {}


def get_music_config(config):
    return config.get("plugins", {}).get("play_music", {})


def get_music_dir(config):
    music_config = get_music_config(config)
    return Path(music_config.get("music_dir", "./music")).resolve()


def get_music_extensions(config):
    configured = get_music_config(config).get("music_ext", DEFAULT_MUSIC_EXTENSIONS)
    return tuple(str(extension).lower() for extension in configured)


def get_track_id(relative_path):
    normalized = Path(relative_path).as_posix()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def build_track_catalog(config):
    music_dir = get_music_dir(config)
    extensions = get_music_extensions(config)
    tracks = {}
    if not music_dir.is_dir():
        return tracks

    for path in music_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        relative_path = path.relative_to(music_dir)
        tracks[get_track_id(relative_path)] = {
            "id": get_track_id(relative_path),
            "path": path,
            "relative_path": relative_path.as_posix(),
            "title": path.stem,
        }
    return tracks


def get_music_stream_base_url(config):
    music_config = get_music_config(config)
    configured_url = str(music_config.get("stream_base_url", "")).strip().rstrip("/")
    if configured_url and "你的" not in configured_url:
        return configured_url

    server_config = config.get("server", {})
    websocket_url = str(server_config.get("websocket", "")).strip()
    parsed = urlparse(websocket_url)
    if parsed.hostname and "你的" not in parsed.hostname:
        scheme = "https" if parsed.scheme == "wss" else "http"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        http_port = int(server_config.get("http_port", 8003))
        return f"{scheme}://{host}:{http_port}"

    return ""


def get_track_stream_url(config, track_id):
    base_url = get_music_stream_base_url(config)
    if not base_url:
        raise ValueError("未配置可供设备访问的音乐 HTTP 地址 stream_base_url")
    return f"{base_url}/music/stream/{track_id}.ogg"


def register_netease_track(config, track):
    music_config = get_music_config(config)
    ttl_seconds = int(music_config.get("remote_url_ttl_seconds", 600))
    token = uuid.uuid4().hex
    _remote_tracks[token] = {
        "track": track,
        "expires_at": time.monotonic() + ttl_seconds,
    }
    return token


def get_netease_track_stream_url(config, token):
    base_url = get_music_stream_base_url(config)
    if not base_url:
        raise ValueError("未配置可供设备访问的音乐 HTTP 地址 stream_base_url")
    return f"{base_url}/music/netease/{token}.ogg"


def get_registered_netease_track(token):
    now = time.monotonic()
    expired_tokens = [
        key for key, value in _remote_tracks.items() if value["expires_at"] <= now
    ]
    for expired_token in expired_tokens:
        _remote_tracks.pop(expired_token, None)

    entry = _remote_tracks.get(token)
    return entry["track"] if entry else None


def get_cache_dir(config):
    music_config = get_music_config(config)
    return Path(music_config.get("cache_dir", "./data/music_cache")).resolve()


async def ensure_cached_ogg(config, track):
    cache_dir = get_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{track['id']}.ogg"
    source = track["path"]

    if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target

    lock = _conversion_locks.setdefault(str(target), asyncio.Lock())
    async with lock:
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法转换音乐文件")

        temporary = cache_dir / f".{track['id']}-{uuid.uuid4().hex}.ogg"
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            "-application",
            "audio",
            "-frame_duration",
            "60",
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            temporary.unlink(missing_ok=True)
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg 转换失败: {detail[-500:]}")

        os.replace(temporary, target)
        return target


class MusicStreamHandler:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.netease = NeteaseOpenApiProvider(config)

    async def handle_get(self, request):
        track_id = request.match_info["track_id"]
        track = build_track_catalog(self.config).get(track_id)
        if track is None:
            raise web.HTTPNotFound(text="Music track not found")

        try:
            cached_file = await ensure_cached_ogg(self.config, track)
        except Exception as error:
            self.logger.bind(tag=__name__).error(f"准备音乐流失败: {error}")
            raise web.HTTPInternalServerError(text="Unable to prepare music stream")

        return web.FileResponse(
            cached_file,
            headers={
                "Content-Type": "audio/ogg",
                "Cache-Control": "public, max-age=86400",
            },
        )

    async def handle_netease_get(self, request):
        token = request.match_info["token"]
        track = get_registered_netease_track(token)
        if track is None:
            raise web.HTTPNotFound(text="Music stream expired")

        try:
            upstream_url = await self.netease.resolve_play_url(track)
            process = await self._start_remote_transcode(upstream_url)
            first_chunk = await asyncio.wait_for(process.stdout.read(16384), timeout=15)
            if not first_chunk:
                await process.wait()
                raise RuntimeError("ffmpeg 没有输出音频数据")
        except Exception as error:
            self.logger.bind(tag=__name__).error(
                f"准备网易云音乐流失败: {type(error).__name__}: {error}"
            )
            raise web.HTTPBadGateway(text="Unable to prepare remote music stream")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/ogg",
                "Cache-Control": "no-store",
            },
        )
        await response.prepare(request)

        try:
            await response.write(first_chunk)
            while True:
                chunk = await process.stdout.read(16384)
                if not chunk:
                    break
                await response.write(chunk)
            await process.wait()
            if process.returncode != 0:
                self.logger.bind(tag=__name__).warning(
                    f"网易云音乐转码提前结束，退出码: {process.returncode}"
                )
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        return response

    async def _start_remote_transcode(self, upstream_url):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法转换音乐流")

        return await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            upstream_url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            "-application",
            "audio",
            "-frame_duration",
            "60",
            "-f",
            "ogg",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
