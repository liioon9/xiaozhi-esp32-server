import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from plugins_func.functions import play_music as music_plugin


class PlayMusicToolTest(unittest.TestCase):
    def test_schema_uses_explicit_mode_and_query_fields(self):
        function = music_plugin.play_music_function_desc["function"]
        parameters = function["parameters"]

        self.assertEqual(parameters["required"], ["mode"])
        self.assertEqual(
            parameters["properties"]["mode"]["enum"],
            ["song", "artist", "daily", "heartbeat", "favorite"],
        )
        self.assertIn("query", parameters["properties"])
        self.assertNotIn("song_name", parameters["properties"])

    def test_song_mode_passes_exact_query_to_search_provider(self):
        track = {
            "id": "encrypted-song-id",
            "originalId": 123,
            "name": "测试歌曲",
            "visible": True,
            "playFlag": True,
            "plLevel": "standard",
        }
        provider = SimpleNamespace(search=AsyncMock(return_value=[track]))
        conn = SimpleNamespace(config={}, logger=Mock())
        cache = {
            "music_config": {"source": "netease"},
            "netease_provider": provider,
        }

        async def run_tool():
            with (
                patch.object(music_plugin, "MUSIC_CACHE", cache),
                patch.object(music_plugin, "initialize_music_handler"),
                patch.object(music_plugin, "_play_netease_track", new_callable=AsyncMock),
            ):
                return await music_plugin.play_music(conn, mode="song", query="晴天")

        result = asyncio.run(run_tool())

        provider.search.assert_awaited_once_with("晴天")
        self.assertEqual(result.response, "正在为您播放音乐")

    def test_shuffle_is_a_music_control_action(self):
        conn = SimpleNamespace(logger=Mock())

        result = asyncio.run(music_plugin.control_music(conn, action="shuffle"))

        self.assertEqual(conn.music_play_mode, "shuffle")
        self.assertEqual(result.response, "好的，已开启随机播放。")


if __name__ == "__main__":
    unittest.main()
