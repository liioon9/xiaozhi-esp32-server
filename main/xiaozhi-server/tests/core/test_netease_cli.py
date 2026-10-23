import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.providers.music.netease_cli import (
    NeteaseCliError,
    NeteaseCliProvider,
    first_playable,
    is_playable,
)


class FakeNeteaseCliProvider(NeteaseCliProvider):
    def __init__(self, config, response):
        super().__init__(config)
        self.response = response
        self.arguments = None

    async def _run(self, *arguments):
        self.arguments = arguments
        return json.dumps(self.response, ensure_ascii=False)


class ResolvingNeteaseCliProvider(NeteaseCliProvider):
    def __init__(self, config, play_url):
        super().__init__(config)
        self.play_url = play_url

    async def _run(self, *arguments):
        encrypted_id = arguments[arguments.index("--encrypted-id") + 1]
        payload = {
            "code": 200,
            "data": {"id": encrypted_id, "playUrl": self.play_url},
        }
        with self.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                "[INFO] GET /openapi/music/basic/song/detail/get/v2 → "
                + json.dumps(payload)
                + "\n"
            )
        return ""


class NeteaseCliProviderTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temporary_directory.name) / "app.log"
        self.config = {
            "plugins": {
                "play_music": {
                    "netease": {"log_path": str(self.log_path)},
                }
            }
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_search_reads_records_from_json_output(self):
        response = {"code": 200, "data": {"records": [{"name": "测试歌曲"}]}}
        provider = FakeNeteaseCliProvider(self.config, response)

        tracks = asyncio.run(provider.search("测试", limit=5))

        self.assertEqual(tracks, [{"name": "测试歌曲"}])
        self.assertIn("测试", provider.arguments)

    def test_only_visible_playable_tracks_are_selected(self):
        blocked = {"visible": False, "playFlag": True, "plLevel": "standard"}
        playable = {"visible": True, "playFlag": True, "plLevel": "standard"}

        self.assertFalse(is_playable(blocked))
        self.assertIs(first_playable([blocked, playable]), playable)

    def test_resolve_play_url_reads_only_new_matching_log_entry(self):
        self.log_path.write_text(
            '[INFO] song/detail/get/v2 → {"data":{"id":"old","playUrl":"old"}}\n',
            encoding="utf-8",
        )
        provider = ResolvingNeteaseCliProvider(
            self.config, "https://music.example.test/temporary.mp3?token=secret"
        )
        track = {"id": "encrypted", "originalId": 123}

        result = asyncio.run(provider.resolve_play_url(track))

        self.assertEqual(
            result, "https://music.example.test/temporary.mp3?token=secret"
        )

    def test_resolve_restarts_stale_daemon_once_then_retries(self):
        provider = NeteaseCliProvider(self.config)
        provider._run = AsyncMock(return_value="")
        provider._wait_for_play_url = AsyncMock(
            side_effect=[
                NeteaseCliError("等待网易云 CLI 写入可播放地址超时"),
                "https://music.example.test/recovered.mp3",
            ]
        )
        provider._recover_player_daemon = AsyncMock()

        result = asyncio.run(
            provider.resolve_play_url({"id": "encrypted", "originalId": 1})
        )

        self.assertEqual(result, "https://music.example.test/recovered.mp3")
        self.assertEqual(provider._run.await_count, 2)
        provider._recover_player_daemon.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
