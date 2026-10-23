import asyncio
import base64
import json
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


OPENAPI_BASE_URL = "https://openapi.music.163.com"
DEVICE_ENVIRONMENT_NAMES = {
    "channel": "NETEASE_DEVICE_CHANNEL",
    "deviceId": "NETEASE_DEVICE_ID",
    "deviceType": "NETEASE_DEVICE_TYPE",
    "appVer": "NETEASE_DEVICE_APP_VER",
    "os": "NETEASE_DEVICE_OS",
    "osVer": "NETEASE_DEVICE_OS_VER",
    "brand": "NETEASE_DEVICE_BRAND",
    "model": "NETEASE_DEVICE_MODEL",
    "clientIp": "NETEASE_DEVICE_CLIENT_IP",
    "netStatus": "NETEASE_DEVICE_NET_STATUS",
}


class NeteaseOpenApiError(RuntimeError):
    pass


def is_playable(track):
    return (
        isinstance(track, dict)
        and track.get("visible") is True
        and track.get("playFlag") is True
        and track.get("plLevel") != "none"
    )


def first_playable(tracks):
    return next((track for track in tracks if is_playable(track)), None)


class NeteaseOpenApiProvider:
    """网易云音乐开放平台 IOT API client.

    Credentials and user tokens deliberately live outside the project config.
    The private key is read from a root-only file and access tokens are kept in
    a root-only token store written by scripts/netease_openapi_login.py.
    """

    def __init__(self, config):
        music_config = config.get("plugins", {}).get("play_music", {})
        self.settings = music_config.get("netease", {})
        self.base_url = str(
            self.settings.get("openapi_base_url", OPENAPI_BASE_URL)
        ).rstrip("/")
        self.timeout = int(self.settings.get("request_timeout", 15))
        self.app_id = self._env("app_id_env", "NETEASE_APP_ID")
        self.app_secret = self._env("app_secret_env", "NETEASE_APP_SECRET")
        self.private_key_path = self._env(
            "private_key_path_env", "NETEASE_PRIVATE_KEY_PATH"
        )
        self.token_store = Path(
            self.settings.get(
                "token_store", "~/.config/xiaozhi/netease_openapi_tokens.json"
            )
        ).expanduser()
        configured_device = dict(self.settings.get("device", {}))
        self.device = {
            key: self._expand_environment(value)
            for key, value in configured_device.items()
        }
        for key, environment_name in DEVICE_ENVIRONMENT_NAMES.items():
            self.device.setdefault(key, os.environ.get(environment_name, ""))
        self._private_key = None

    def _env(self, setting_name, default_name):
        return os.environ.get(str(self.settings.get(setting_name, default_name)), "").strip()

    @staticmethod
    def _expand_environment(value):
        if not isinstance(value, str):
            return value
        if value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
        return value

    def _validate_app_configuration(self):
        missing = []
        if not self.app_id:
            missing.append("NETEASE_APP_ID")
        if not self.app_secret:
            missing.append("NETEASE_APP_SECRET")
        if not self.private_key_path:
            missing.append("NETEASE_PRIVATE_KEY_PATH")
        if missing:
            raise NeteaseOpenApiError(
                "网易云开放平台未配置：" + "、".join(missing)
            )

        required_device_fields = (
            "channel",
            "deviceId",
            "deviceType",
            "appVer",
            "os",
            "osVer",
            "brand",
            "model",
            "clientIp",
            "netStatus",
        )
        absent = [key for key in required_device_fields if not self.device.get(key)]
        if absent:
            raise NeteaseOpenApiError(
                "网易云设备信息缺少：" + "、".join(absent)
            )

    def _load_private_key(self):
        if self._private_key is not None:
            return self._private_key

        self._validate_app_configuration()
        key_path = Path(self.private_key_path).expanduser()
        try:
            key_bytes = key_path.read_bytes().strip()
        except OSError as error:
            raise NeteaseOpenApiError("无法读取网易云 RSA 私钥文件") from error

        try:
            if b"-----BEGIN" in key_bytes:
                private_key = serialization.load_pem_private_key(
                    key_bytes, password=None
                )
            else:
                private_key = serialization.load_der_private_key(
                    base64.b64decode(key_bytes), password=None
                )
        except (ValueError, TypeError) as error:
            raise NeteaseOpenApiError("网易云 RSA 私钥格式无效") from error

        self._private_key = private_key
        return private_key

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _sign(self, parameters):
        content = "&".join(
            f"{key}={parameters[key]}"
            for key in sorted(parameters)
            if key != "sign" and parameters[key] not in (None, "")
        )
        signature = self._load_private_key().sign(
            content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(signature).decode("ascii")

    def _read_tokens(self):
        try:
            tokens = json.loads(self.token_store.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise NeteaseOpenApiError(
                "网易云账号尚未登录，请运行 netease_openapi_login.py"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise NeteaseOpenApiError("网易云登录令牌文件无效") from error

        if not tokens.get("accessToken"):
            raise NeteaseOpenApiError("网易云账号尚未登录，请重新扫码登录")
        return tokens

    def _write_tokens(self, tokens):
        self.token_store.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_store.with_suffix(".tmp")
        temporary.write_text(self._json(tokens), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.token_store)

    def _request_sync(self, path, biz_content, access_token=None):
        self._validate_app_configuration()
        parameters = {
            "appId": self.app_id,
            "appSecret": self.app_secret,
            "bizContent": self._json(biz_content),
            "device": self._json(self.device),
            "signType": "RSA_SHA256",
            "timestamp": str(int(time.time() * 1000)),
        }
        if access_token:
            parameters["accessToken"] = access_token
        parameters["sign"] = self._sign(parameters)

        try:
            response = requests.get(
                f"{self.base_url}{path}", params=parameters, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise NeteaseOpenApiError("网易云开放平台请求失败") from error

        return payload

    async def _request(self, path, biz_content, access_token=None):
        return await asyncio.to_thread(
            self._request_sync, path, biz_content, access_token
        )

    async def _authenticated_request(self, path, biz_content):
        tokens = self._read_tokens()
        payload = await self._request(path, biz_content, tokens["accessToken"])
        if payload.get("code") == 200:
            return payload
        if payload.get("code") != 1406:
            self._raise_api_error(payload)

        refreshed = await self.refresh_tokens(tokens)
        payload = await self._request(path, biz_content, refreshed["accessToken"])
        if payload.get("code") != 200:
            self._raise_api_error(payload)
        return payload

    @staticmethod
    def _raise_api_error(payload):
        raise NeteaseOpenApiError(
            str(payload.get("message") or "网易云开放平台返回异常")
        )

    async def refresh_tokens(self, tokens=None):
        tokens = tokens or self._read_tokens()
        refresh_token = tokens.get("refreshToken")
        if not refresh_token:
            raise NeteaseOpenApiError("网易云登录已过期，请重新扫码登录")
        payload = await self._request(
            "/openapi/music/basic/user/oauth2/token/refresh/v2",
            {
                "clientId": self.app_id,
                "clientSecret": self.app_secret,
                "refreshToken": refresh_token,
            },
            tokens.get("accessToken"),
        )
        if payload.get("code") != 200:
            self._raise_api_error(payload)
        data = payload.get("data") or {}
        if not data.get("accessToken") or not data.get("refreshToken"):
            raise NeteaseOpenApiError("网易云登录刷新失败，请重新扫码登录")
        data["expiresAt"] = int(time.time()) + int(data.get("expiresTime", 604800))
        self._write_tokens(data)
        return data

    async def search(self, keyword, limit=30):
        payload = await self._authenticated_request(
            "/openapi/music/basic/search/song/get/v3",
            {"keyword": keyword, "limit": limit, "offset": 0},
        )
        return self._records(payload.get("data"))

    async def daily(self, limit=30):
        payload = await self._authenticated_request(
            "/openapi/music/basic/recommend/songlist/get/v2", {"limit": limit}
        )
        return self._records(payload.get("data"))

    async def favorite_tracks(self, limit=100):
        favorite = await self._authenticated_request(
            "/openapi/music/basic/playlist/star/get/v2", {"limit": 1, "offset": 0}
        )
        favorite_data = favorite.get("data") or {}
        playlist_id = favorite_data.get("id")
        if not playlist_id:
            raise NeteaseOpenApiError("没有获取到红心歌单")
        tracks = await self._authenticated_request(
            "/openapi/music/basic/playlist/song/list/get/v3",
            {"playlistId": playlist_id, "limit": min(limit, 500), "offset": 0},
        )
        return self._records(tracks.get("data"))

    async def resolve_play_url(self, track):
        song_id = track.get("id")
        if not song_id:
            raise NeteaseOpenApiError("歌曲缺少网易云歌曲 ID")
        payload = await self._authenticated_request(
            "/openapi/music/basic/song/playurl/get/v2",
            {"songId": song_id, "bitrate": 320},
        )
        data = payload.get("data") or {}
        if payload.get("subCode") not in (None, "200", 200) or not data.get("url"):
            raise NeteaseOpenApiError(
                str(payload.get("message") or "歌曲受版权或会员权益限制")
            )
        return data["url"]

    async def create_qr_login(self):
        anonymous = await self._request(
            "/openapi/music/basic/oauth2/login/anonymous", {"clientId": self.app_id}
        )
        if anonymous.get("code") != 200:
            self._raise_api_error(anonymous)
        anonymous_token = (anonymous.get("data") or {}).get("accessToken")
        if not anonymous_token:
            raise NeteaseOpenApiError("无法创建网易云匿名登录会话")
        qr = await self._request(
            "/openapi/music/basic/user/oauth2/qrcodekey/get/v2",
            {"type": 2, "expiredKey": "300"},
            anonymous_token,
        )
        if qr.get("code") != 200:
            self._raise_api_error(qr)
        data = qr.get("data") or {}
        if not data.get("qrCodeUrl") or not data.get("uniKey"):
            raise NeteaseOpenApiError("无法获取网易云登录二维码")
        return anonymous_token, data

    async def poll_qr_login(self, anonymous_token, unikey):
        payload = await self._request(
            "/openapi/music/basic/oauth2/device/login/qrcode/get",
            {"key": unikey, "clientId": self.app_id},
            anonymous_token,
        )
        if payload.get("code") != 200:
            self._raise_api_error(payload)
        data = payload.get("data") or {}
        if data.get("status") == 803:
            token_data = data.get("accessToken") or {}
            if not token_data.get("accessToken") or not token_data.get("refreshToken"):
                raise NeteaseOpenApiError("网易云扫码登录没有返回令牌")
            token_data["expiresAt"] = int(time.time()) + int(
                token_data.get("expireTime", 604800)
            )
            self._write_tokens(token_data)
        return data

    @staticmethod
    def _records(data):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("records", "songs", "songList"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
