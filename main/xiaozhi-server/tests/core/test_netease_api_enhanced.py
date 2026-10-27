import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.providers.music.netease_api_enhanced import NeteaseApiEnhancedProvider
from core.providers.music.netease_cli import NeteaseCliError
from core.providers.music.netease_provider import create_netease_provider


class NeteaseApiEnhancedProviderTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "plugins": {
                "play_music": {
                    "netease": {
                        "provider": "api_enhanced",
                        "api_base_url": "http://127.0.0.1:3000",
                    }
                }
            }
        }

    def test_factory_uses_api_enhanced_when_configured(self):
        self.assertIsInstance(
            create_netease_provider(self.config), NeteaseApiEnhancedProvider
        )

    def test_search_normalizes_api_song_shape(self):
        provider = NeteaseApiEnhancedProvider(self.config)
        provider._request = AsyncMock(
            return_value={
                "code": 200,
                "result": {
                    "songs": [
                        {"id": 1, "name": "测试歌", "artists": [{"name": "歌手"}]}
                    ]
                },
            }
        )

        tracks = asyncio.run(provider.search("测试", limit=5))

        self.assertEqual(
            tracks,
            [
                {
                    "id": "1",
                    "originalId": 1,
                    "name": "测试歌",
                    "artists": [{"name": "歌手"}],
                    "visible": True,
                    "playFlag": True,
                    "plLevel": "standard",
                }
            ],
        )
        provider._request.assert_awaited_once_with(
            "search", keywords="测试", limit=5, type=1
        )

    def test_resolve_play_url_forces_unblock_off(self):
        provider = NeteaseApiEnhancedProvider(self.config)
        provider._request = AsyncMock(
            return_value={"code": 200, "data": [{"url": "https://music.example/song.mp3"}]}
        )

        url = asyncio.run(provider.resolve_play_url({"id": "1", "originalId": 1}))

        self.assertEqual(url, "https://music.example/song.mp3")
        provider._request.assert_awaited_once_with(
            "song/url/v1", id=1, level="standard", unblock="false"
        )

    def test_resolve_play_url_rejects_missing_url(self):
        provider = NeteaseApiEnhancedProvider(self.config)
        provider._request = AsyncMock(return_value={"code": 200, "data": [{"url": None}]})

        with self.assertRaisesRegex(NeteaseCliError, "没有获取到可播放地址"):
            asyncio.run(provider.resolve_play_url({"id": "1", "originalId": 1}))

    def test_request_reads_cookie_only_from_environment(self):
        provider = NeteaseApiEnhancedProvider(self.config)

        with patch.dict(os.environ, {"NETEASE_COOKIE": "test-cookie"}, clear=False), patch(
            "core.providers.music.netease_api_enhanced.aiohttp.ClientSession"
        ) as session_class:
            response = AsyncMock()
            response.status = 200
            response.json = AsyncMock(return_value={"code": 200})
            session = MagicMock()
            session.get.return_value.__aenter__.return_value = response
            session_class.return_value.__aenter__.return_value = session

            asyncio.run(provider._request("search", keywords="test"))

        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {"keywords": "test", "cookie": "test-cookie"},
        )


if __name__ == "__main__":
    unittest.main()
