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
        self.play_url_wait_seconds = float(
            netease_config.get("play_url_wait_seconds", 10)
        )
        self.recover_stale_daemon = bool(
            netease_config.get("recover_stale_daemon", True)
        )
        self.daemon_socket_path = Path(
            netease_config.get(
                "daemon_socket_path", "~/.config/ncm-cli/player-daemon.sock"
            )
        ).expanduser()
        self.daemon_process_pattern = str(
            netease_config.get(
                "daemon_process_pattern", "@music163/ncm-cli/dist/index.js"
            )
        )
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
            for attempt in range(2):
                start_offset = (
                    self.log_path.stat().st_size if self.log_path.exists() else 0
                )
                try:
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
                    return await self._wait_for_play_url(start_offset, encrypted_id)
                except NeteaseCliError:
                    if attempt or not self.recover_stale_daemon:
                        raise
                    await self._recover_player_daemon()

    async def _recover_player_daemon(self):
        """Clear only the CLI's stale local player daemon before one retry."""
        process = await asyncio.create_subprocess_exec(
            "pkill",
            "-f",
            self.daemon_process_pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate()
        try:
            self.daemon_socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise NeteaseCliError("无法清理网易云 CLI 播放器守护进程") from error
        await asyncio.sleep(0.3)

    async def _wait_for_play_url(self, start_offset, encrypted_id):
        deadline = asyncio.get_running_loop().time() + self.play_url_wait_seconds
        while True:
            try:
                return self._read_play_url(start_offset, encrypted_id)
            except NeteaseCliError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise NeteaseCliError(
                        "等待网易云 CLI 写入可播放地址超时"
                    )
                await asyncio.sleep(0.2)

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
