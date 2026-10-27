import asyncio
import os
import time

import aiohttp

from core.providers.music.netease_cli import NeteaseCliError


class NeteaseApiEnhancedProvider:
    """Adapter for a privately deployed api-enhanced service.

    This adapter deliberately only uses normal playback endpoints. It never
    requests API Enhanced's proxy, random-IP, or unblock capabilities.
    """

    def __init__(self, config):
        music_config = config.get("plugins", {}).get("play_music", {})
        netease_config = music_config.get("netease", {})
        self.base_url = str(netease_config.get("api_base_url", "")).rstrip("/")
        self.timeout_seconds = int(netease_config.get("api_timeout_seconds", 15))
        self.cookie_env = str(netease_config.get("cookie_env", "NETEASE_COOKIE"))
        self.play_level = str(netease_config.get("play_level", "standard"))
        self.metadata_cache_ttl_seconds = int(
            netease_config.get("metadata_cache_ttl_seconds", 1800)
        )
        self.favorite_tracks_cache_ttl_seconds = int(
            netease_config.get("favorite_tracks_cache_ttl_seconds", 300)
        )
        self._user_id_cache = None
        self._user_id_cache_expires_at = 0.0
        self._favorite_playlist_id_cache = None
        self._favorite_playlist_id_cache_expires_at = 0.0
        self._favorite_tracks_cache = None
        self._favorite_tracks_cache_expires_at = 0.0
        self._account_lock = asyncio.Lock()
        self._favorite_tracks_lock = asyncio.Lock()

    async def _request(self, path, **params):
        if not self.base_url or "你的" in self.base_url:
            raise NeteaseCliError("未配置网易云 API Enhanced 地址")

        cookie = os.environ.get(self.cookie_env)
        headers = {"Cookie": cookie} if cookie else None

        url = f"{self.base_url}/{path.lstrip('/')}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params, headers=headers) as response:
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        message = payload.get("message") if isinstance(payload, dict) else ""
                        raise NeteaseCliError(message or f"网易云 API 请求失败（HTTP {response.status}）")
        except asyncio.TimeoutError as error:
            raise NeteaseCliError("网易云 API 请求超时") from error
        except aiohttp.ClientError as error:
            raise NeteaseCliError("无法连接网易云 API Enhanced 服务") from error
        except ValueError as error:
            raise NeteaseCliError("网易云 API 返回了无法解析的数据") from error

        if not isinstance(payload, dict):
            raise NeteaseCliError("网易云 API 返回了无法解析的数据")
        if payload.get("code") not in (None, 200):
            raise NeteaseCliError(payload.get("message") or "网易云 API 请求失败")
        return payload

    @staticmethod
    def _extract_tracks(payload):
        data = payload.get("data", {})
        result = payload.get("result", {})
        candidates = (
            payload.get("songs") if isinstance(payload.get("songs"), list) else None,
            data.get("dailySongs") if isinstance(data, dict) else None,
            data.get("songs") if isinstance(data, dict) else None,
            data.get("tracks") if isinstance(data, dict) else None,
            data if isinstance(data, list) else None,
            result.get("songs") if isinstance(result, dict) else None,
        )
        for tracks in candidates:
            if isinstance(tracks, list):
                return tracks
        return []

    @staticmethod
    def _normalize_track(track):
        # ``playmode/intelligence/list`` wraps each recommendation in a
        # ``songInfo`` object.  Its outer item only carries recommendation
        # metadata, so reading it directly loses the song name and artists.
        if isinstance(track.get("songInfo"), dict):
            track = track["songInfo"]
        song_id = track.get("id")
        if song_id is None:
            return None
        artists = track.get("artists") or track.get("ar") or []
        normalized_artists = [
            {"name": artist.get("name", "")}
            for artist in artists
            if isinstance(artist, dict) and artist.get("name")
        ]
        unavailable = bool(track.get("noCopyrightRcmd"))
        return {
            "id": str(song_id),
            "originalId": int(song_id),
            "name": track.get("name") or "网易云音乐",
            "artists": normalized_artists,
            "visible": not unavailable,
            "playFlag": not unavailable,
            "plLevel": "none" if unavailable else "standard",
        }

    @classmethod
    def _normalize_tracks(cls, tracks):
        return [normalized for track in tracks if (normalized := cls._normalize_track(track))]

    async def search(self, keyword, limit=30):
        payload = await self._request("search", keywords=keyword, limit=limit, type=1)
        return self._normalize_tracks(self._extract_tracks(payload))

    async def daily(self, limit=30):
        payload = await self._request("recommend/songs")
        return self._normalize_tracks(self._extract_tracks(payload))[:limit]

    async def _user_id(self):
        if self._user_id_cache and time.monotonic() < self._user_id_cache_expires_at:
            return self._user_id_cache
        async with self._account_lock:
            if self._user_id_cache and time.monotonic() < self._user_id_cache_expires_at:
                return self._user_id_cache
            payload = await self._request("user/account")
            profile = payload.get("profile", {})
            user_id = profile.get("userId") or payload.get("account", {}).get("id")
            if not user_id:
                raise NeteaseCliError("无法获取网易云账号信息，请先登录")
            self._user_id_cache = user_id
            self._user_id_cache_expires_at = (
                time.monotonic() + self.metadata_cache_ttl_seconds
            )
            return user_id

    async def favorite_playlist_id(self):
        if (
            self._favorite_playlist_id_cache
            and time.monotonic() < self._favorite_playlist_id_cache_expires_at
        ):
            return self._favorite_playlist_id_cache
        user_id = await self._user_id()
        async with self._account_lock:
            if (
                self._favorite_playlist_id_cache
                and time.monotonic() < self._favorite_playlist_id_cache_expires_at
            ):
                return self._favorite_playlist_id_cache
            payload = await self._request("user/playlist", uid=user_id, limit=1000)
            for playlist in payload.get("playlist", []):
                if playlist.get("specialType") == 5:
                    self._favorite_playlist_id_cache = playlist.get("id")
                    self._favorite_playlist_id_cache_expires_at = (
                        time.monotonic() + self.metadata_cache_ttl_seconds
                    )
                    return self._favorite_playlist_id_cache
            raise NeteaseCliError("没有获取到红心歌单")

    async def favorite_tracks(self, limit=1000):
        """Return the red-heart playlist in the order shown by NetEase Cloud Music.

        ``likelist`` is only an ID set/list and does not model the user's
        playlist ordering. ``playlist/track/all`` instead starts from the
        playlist's ordered ``trackIds`` list, so queue indexes map to the
        same next/previous order as the NetEase client.
        """
        cache_is_valid = (
            self._favorite_tracks_cache is not None
            and time.monotonic() < self._favorite_tracks_cache_expires_at
        )
        if cache_is_valid:
            return self._favorite_tracks_cache[:limit]
        async with self._favorite_tracks_lock:
            cache_is_valid = (
                self._favorite_tracks_cache is not None
                and time.monotonic() < self._favorite_tracks_cache_expires_at
            )
            if cache_is_valid:
                return self._favorite_tracks_cache[:limit]
            playlist_id = await self.favorite_playlist_id()
            payload = await self._request(
                "playlist/track/all", id=playlist_id, limit=1000, offset=0
            )
            self._favorite_tracks_cache = self._normalize_tracks(self._extract_tracks(payload))
            self._favorite_tracks_cache_expires_at = (
                time.monotonic() + self.favorite_tracks_cache_ttl_seconds
            )
            return self._favorite_tracks_cache[:limit]

    async def search_artist_tracks(self, artist_name, limit=30):
        payload = await self._request("search", keywords=artist_name, limit=1, type=100)
        artists = payload.get("result", {}).get("artists", [])
        if not artists:
            return []
        payload = await self._request("artist/songs", id=artists[0].get("id"), limit=limit)
        return self._normalize_tracks(self._extract_tracks(payload))[:limit]

    async def heartbeat(self, playlist_id, song_id, limit=30):
        payload = await self._request(
            "playmode/intelligence/list", id=song_id, pid=playlist_id, count=limit
        )
        return self._normalize_tracks(self._extract_tracks(payload))[:limit]

    async def like_track(self, track):
        song_id = track.get("originalId") or track.get("id")
        if not song_id:
            raise NeteaseCliError("歌曲缺少收藏 ID")
        await self._request("like", id=song_id, like="true")
        self._favorite_tracks_cache_expires_at = 0.0

    async def dislike_track(self, track):
        song_id = track.get("originalId") or track.get("id")
        if not song_id:
            raise NeteaseCliError("歌曲缺少收藏 ID")
        await self._request("like", id=song_id, like="false")
        self._favorite_tracks_cache_expires_at = 0.0

    async def resolve_play_url(self, track):
        song_id = track.get("originalId") or track.get("id")
        if not song_id:
            raise NeteaseCliError("歌曲缺少播放 ID")
        payload = await self._request(
            "song/url/v1",
            id=song_id,
            level=self.play_level,
            unblock="false",
        )
        entries = payload.get("data", [])
        if not isinstance(entries, list) or not entries or not entries[0].get("url"):
            raise NeteaseCliError("没有获取到可播放地址，歌曲可能受版权或会员限制")
        return entries[0]["url"]
