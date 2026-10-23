import asyncio
import tempfile
import unittest
import wave
from pathlib import Path

from core.api.music_handler import (
    build_track_catalog,
    ensure_cached_ogg,
    get_music_stream_base_url,
    get_netease_track_stream_url,
    get_registered_netease_track,
    get_track_stream_url,
    register_netease_track,
)


class MusicHandlerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config = {
            "server": {
                "http_port": 8003,
                "websocket": "ws://192.0.2.10:8000/xiaozhi/v1/",
            },
            "plugins": {
                "play_music": {
                    "music_dir": str(self.root / "music"),
                    "cache_dir": str(self.root / "cache"),
                    "music_ext": [".wav"],
                }
            },
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_silent_wav(self, relative_path):
        path = self.root / "music" / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\0\0" * 2400)

    def test_catalog_uses_opaque_stable_track_id(self):
        self.write_silent_wav("album/song.wav")

        catalog = build_track_catalog(self.config)

        self.assertEqual(len(catalog), 1)
        track = next(iter(catalog.values()))
        self.assertEqual(track["relative_path"], "album/song.wav")
        self.assertEqual(track["title"], "song")
        self.assertNotIn("/", track["id"])
        self.assertNotIn("song", track["id"])

    def test_stream_url_defaults_to_websocket_host_and_http_port(self):
        self.assertEqual(
            get_music_stream_base_url(self.config), "http://192.0.2.10:8003"
        )
        self.assertEqual(
            get_track_stream_url(self.config, "abc123"),
            "http://192.0.2.10:8003/music/stream/abc123.ogg",
        )

    def test_audio_is_converted_once_and_cached(self):
        self.write_silent_wav("song.wav")
        track = next(iter(build_track_catalog(self.config).values()))

        first = asyncio.run(ensure_cached_ogg(self.config, track))
        first_mtime = first.stat().st_mtime_ns
        second = asyncio.run(ensure_cached_ogg(self.config, track))

        self.assertEqual(first, second)
        self.assertTrue(second.read_bytes().startswith(b"OggS"))
        self.assertEqual(second.stat().st_mtime_ns, first_mtime)

    def test_netease_stream_uses_opaque_temporary_token(self):
        track = {"id": "encrypted-song-id", "originalId": 123, "name": "song"}

        token = register_netease_track(self.config, track)

        self.assertNotIn("encrypted-song-id", token)
        self.assertEqual(get_registered_netease_track(token), track)
        self.assertEqual(
            get_netease_track_stream_url(self.config, token),
            f"http://192.0.2.10:8003/music/netease/{token}.ogg",
        )


if __name__ == "__main__":
    unittest.main()
