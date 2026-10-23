import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.providers.music.netease_openapi import NeteaseOpenApiProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class NeteaseOpenApiProviderTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_der = private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.key_path = root / "private.key"
        self.key_path.write_bytes(base64.b64encode(key_der))
        self.tokens_path = root / "tokens.json"
        self.tokens_path.write_text(
            json.dumps({"accessToken": "access", "refreshToken": "refresh"}),
            encoding="utf-8",
        )
        self.private_key = private_key
        self.environment = {
            "NETEASE_APP_ID": "app-id",
            "NETEASE_APP_SECRET": "app-secret",
            "NETEASE_PRIVATE_KEY_PATH": str(self.key_path),
        }
        self.config = {
            "plugins": {
                "play_music": {
                    "netease": {
                        "token_store": str(self.tokens_path),
                        "device": {
                            "channel": "test",
                            "deviceId": "device-1",
                            "deviceType": "speaker",
                            "appVer": "1.0.0",
                            "os": "linux",
                            "osVer": "1",
                            "brand": "test",
                            "model": "test",
                            "clientIp": "127.0.0.1",
                            "netStatus": "wifi",
                        },
                    }
                }
            }
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def provider(self):
        return NeteaseOpenApiProvider(self.config)

    def test_request_signs_raw_json_parameters_before_url_encoding(self):
        with patch.dict(os.environ, self.environment, clear=False), patch(
            "core.providers.music.netease_openapi.requests.get"
        ) as mocked_get:
            mocked_get.return_value = FakeResponse({"code": 200, "data": {}})
            asyncio.run(self.provider()._request("/test", {"songId": "song-id"}))

        parameters = mocked_get.call_args.kwargs["params"]
        content = "&".join(
            f"{key}={parameters[key]}"
            for key in sorted(parameters)
            if key != "sign" and parameters[key] not in (None, "")
        )
        self.private_key.public_key().verify(
            base64.b64decode(parameters["sign"]),
            content.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        self.assertEqual(json.loads(parameters["bizContent"]), {"songId": "song-id"})

    def test_resolve_play_url_uses_official_song_endpoint(self):
        with patch.dict(os.environ, self.environment, clear=False), patch.object(
            NeteaseOpenApiProvider, "_authenticated_request", new_callable=AsyncMock
        ) as request:
            request.return_value = {
                "code": 200,
                "subCode": "200",
                "data": {"url": "https://music.example.test/song.mp3"},
            }
            url = asyncio.run(self.provider().resolve_play_url({"id": "encrypted-id"}))

        self.assertEqual(url, "https://music.example.test/song.mp3")
        self.assertEqual(
            request.call_args.args,
            (
                "/openapi/music/basic/song/playurl/get/v2",
                {"songId": "encrypted-id", "bitrate": 320},
            ),
        )


if __name__ == "__main__":
    unittest.main()
