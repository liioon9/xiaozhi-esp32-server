import asyncio
import json
from pathlib import Path


class NeteaseCliError(RuntimeError):
    pass


def is_playable(track):
    return (
        isinstance(track, dict)
        and track.get("visible") is True
        and track.get("playFlag") is True
        and track.get("plLevel") != "none"
    )


class NeteaseCliProvider:
    def __init__(self, config):
        music_config = config.get("plugins", {}).get("play_music", {})
        netease_config = music_config.get("netease", {})
        self.cli_command = str(netease_config.get("cli_command", "ncm-cli"))
        self.log_path = Path(
            netease_config.get("log_path", "~/.config/ncm-cli/app.log")
        ).expanduser()
        self.command_timeout = int(netease_config.get("command_timeout", 15))
        self._resolve_lock = asyncio.Lock()

    async def _run_json(self, *arguments):
        stdout = await self._run(*arguments, "--output", "json")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise NeteaseCliError("网易云 CLI 返回了无法解析的数据") from error

        if payload.get("code") not in (None, 200) or payload.get("success") is False:
            raise NeteaseCliError(payload.get("message") or "网易云 CLI 请求失败")
        return payload

    async def _run(self, *arguments):
        try:
            process = await asyncio.create_subprocess_exec(
                self.cli_command,
                *[str(argument) for argument in arguments],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise NeteaseCliError("未安装 ncm-cli") from error

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.command_timeout
            )
        except asyncio.TimeoutError as error:
            process.kill()
            await process.wait()
            raise NeteaseCliError("网易云 CLI 请求超时") from error

        if process.returncode != 0:
            raise NeteaseCliError(
                f"网易云 CLI 命令执行失败（退出码 {process.returncode}）"
            )
        return stdout.decode("utf-8", errors="replace").strip()

    async def search(self, keyword, limit=30):
        payload = await self._run_json(
            "search", "song", "--keyword", keyword, "--limit", limit
        )
        return payload.get("data", {}).get("records", [])

    async def daily(self, limit=30):
        payload = await self._run_json("recommend", "daily", "--limit", limit)
        return payload.get("data", [])

    async def favorite_tracks(self, limit=100):
        favorite = await self._run_json("user", "favorite")
        playlist_id = favorite.get("data", {}).get("id")
        if not playlist_id:
            raise NeteaseCliError("没有获取到红心歌单")

        tracks = await self._run_json(
            "playlist",
            "tracks",
            "--playlistId",
            playlist_id,
            "--limit",
            limit,
        )
        data = tracks.get("data", {})
        if isinstance(data, list):
            return data
        return data.get("records", data.get("songs", []))

    async def resolve_play_url(self, track):
        encrypted_id = track.get("id")
        original_id = track.get("originalId")
        if not encrypted_id or not original_id:
            raise NeteaseCliError("歌曲缺少播放 ID")

        async with self._resolve_lock:
            start_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
            await self._run(
                "play",
                "--song",
                "--encrypted-id",
                encrypted_id,
                "--original-id",
                original_id,
                "--output",
                "json",
            )
            return self._read_play_url(start_offset, encrypted_id)

    def _read_play_url(self, start_offset, encrypted_id):
        if not self.log_path.is_file():
            raise NeteaseCliError("网易云 CLI 没有生成播放日志")

        file_size = self.log_path.stat().st_size
        if start_offset > file_size:
            start_offset = 0
        with self.log_path.open("r", encoding="utf-8", errors="replace") as log_file:
            log_file.seek(start_offset)
            appended = log_file.read()

        for line in reversed(appended.splitlines()):
            if "song/detail/get/v2" not in line or "→" not in line:
                continue
            try:
                payload = json.loads(line.split("→", 1)[1].strip())
            except (json.JSONDecodeError, IndexError):
                continue
            data = payload.get("data", {})
            if data.get("id") == encrypted_id and data.get("playUrl"):
                return data["playUrl"]

        raise NeteaseCliError("没有获取到可播放地址，歌曲可能受版权或会员限制")


def first_playable(tracks):
    return next((track for track in tracks if is_playable(track)), None)
