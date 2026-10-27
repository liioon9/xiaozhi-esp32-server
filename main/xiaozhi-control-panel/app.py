"""A local-only control panel for a self-hosted xiaozhi-server.

It intentionally has no authentication because it is designed to listen on
127.0.0.1 only. Reach it remotely through an SSH tunnel or a separately
configured reverse proxy with an IP allowlist.
"""

import argparse
import asyncio
from copy import deepcopy
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from ruamel.yaml import YAML


ALLOWED_SERVICES = {"xiaozhi-server", "ncm-api"}
ALLOWED_LEVELS = {"standard", "higher", "exhigh", "lossless", "hires", "jyeffect", "sky", "jymaster"}
QR_TTL_SECONDS = 300
REDACTED = "<REDACTED>"
SENSITIVE_KEY_MARKERS = ("api_key", "secret", "token", "password", "authorization", "cookie", "credential", "private_key")
MISSING = object()


class ControlPanel:
    def __init__(self, config_path, netease_env_path):
        self.config_path = Path(config_path)
        self.base_config_path = self.config_path.parent.parent / "config.yaml"
        self.netease_env_path = Path(netease_env_path)
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.qr_session = None

    def _load_yaml(self, path):
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file:
            return self.yaml.load(file) or {}

    def _load_config(self):
        return self._load_yaml(self.config_path)

    @staticmethod
    def _merge_config(base, override):
        if not isinstance(base, dict) or not isinstance(override, dict):
            return deepcopy(override)
        merged = deepcopy(base)
        for key, value in override.items():
            merged[key] = ControlPanel._merge_config(merged[key], value) if key in merged else deepcopy(value)
        return merged

    @staticmethod
    def _config_diff(base, value):
        if isinstance(base, dict) and isinstance(value, dict):
            diff = {}
            for key, item in value.items():
                if key not in base:
                    diff[key] = deepcopy(item)
                    continue
                nested = ControlPanel._config_diff(base[key], item)
                if nested is not MISSING:
                    diff[key] = nested
            return diff if diff else MISSING
        return MISSING if base == value else deepcopy(value)

    def _effective_config(self):
        return self._merge_config(self._load_yaml(self.base_config_path), self._load_config())

    def _save_config(self, config):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            backup = self.config_path.with_name(
                f"{self.config_path.name}.control-panel-{datetime.now():%Y%m%d%H%M%S}.bak"
            )
            backup.write_bytes(self.config_path.read_bytes())
            os.chmod(backup, 0o600)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.config_path.parent, delete=False
        ) as file:
            self.yaml.dump(config, file)
            temp_name = file.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.config_path)

    @staticmethod
    def _is_sensitive_key(key):
        normalized = str(key).lower()
        if normalized.endswith("_env"):
            return False
        return normalized == "mcp_endpoint" or any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)

    def _redact_config(self, value, key=None):
        if key is not None and self._is_sensitive_key(key):
            return REDACTED
        if isinstance(value, dict):
            return {item_key: self._redact_config(item_value, item_key) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [self._redact_config(item) for item in value]
        return value

    def _restore_redacted_values(self, submitted, existing):
        if submitted == REDACTED and existing is not None:
            return existing
        if isinstance(submitted, dict):
            existing = existing if isinstance(existing, dict) else {}
            return {
                key: self._restore_redacted_values(value, existing.get(key))
                for key, value in submitted.items()
            }
        if isinstance(submitted, list):
            existing = existing if isinstance(existing, list) else []
            return [
                self._restore_redacted_values(value, existing[index] if index < len(existing) else None)
                for index, value in enumerate(submitted)
            ]
        return submitted

    def get_redacted_config_text(self):
        stream = StringIO()
        self.yaml.dump(self._redact_config(self._effective_config()), stream)
        return stream.getvalue()

    def update_config_text(self, text):
        if not isinstance(text, str) or len(text) > 1024 * 1024:
            raise ValueError("配置内容无效")
        try:
            submitted = self.yaml.load(text) or {}
        except Exception as error:
            raise ValueError("YAML 格式无效") from error
        if not isinstance(submitted, dict):
            raise ValueError("配置根节点必须是对象")
        restored = self._restore_redacted_values(submitted, self._effective_config())
        diff = self._config_diff(self._load_yaml(self.base_config_path), restored)
        self._save_config({} if diff is MISSING else diff)

    @staticmethod
    def _music_config(config):
        plugins = config.setdefault("plugins", {})
        music = plugins.setdefault("play_music", {})
        return music, music.setdefault("netease", {})

    def get_music_config(self):
        config = self._load_config()
        music, netease = self._music_config(config)
        return {
            "source": music.get("source", "netease"),
            "provider": netease.get("provider", "api_enhanced"),
            "api_base_url": netease.get("api_base_url", "http://127.0.0.1:3000"),
            "api_timeout_seconds": int(netease.get("api_timeout_seconds", 15)),
            "play_level": netease.get("play_level", "standard"),
        }

    def update_music_config(self, values):
        source = values.get("source")
        provider = values.get("provider")
        api_base_url = values.get("api_base_url")
        timeout = values.get("api_timeout_seconds")
        play_level = values.get("play_level")

        if source not in {"netease", "local"}:
            raise ValueError("source 只能是 netease 或 local")
        if provider != "api_enhanced":
            raise ValueError("控制台只支持 api_enhanced 提供方")
        if not isinstance(api_base_url, str):
            raise ValueError("api_base_url 必须是地址")
        parsed = urlparse(api_base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("API 地址只能指向本机服务")
        if not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ValueError("请求超时必须是 1 到 60 秒")
        if play_level not in ALLOWED_LEVELS:
            raise ValueError("不支持的音质档位")

        config = self._load_config()
        music, netease = self._music_config(config)
        music["source"] = source
        netease["provider"] = provider
        netease["api_base_url"] = api_base_url.rstrip("/")
        netease["api_timeout_seconds"] = timeout
        netease["play_level"] = play_level
        self._save_config(config)

    def is_netease_bound(self):
        if not self.netease_env_path.exists():
            return False
        return any(
            line.startswith("NETEASE_COOKIE=") and len(line.strip()) > len("NETEASE_COOKIE=")
            for line in self.netease_env_path.read_text(encoding="utf-8").splitlines()
        )

    def save_netease_cookie(self, cookie):
        if not isinstance(cookie, str) or not cookie.strip() or "\n" in cookie or "\r" in cookie:
            raise ValueError("网易云登录信息无效")
        self.netease_env_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.netease_env_path.parent, delete=False
        ) as file:
            file.write(f"NETEASE_COOKIE={cookie.strip()}\n")
            temp_name = file.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.netease_env_path)

    def clear_netease_cookie(self):
        self.netease_env_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.netease_env_path.parent, delete=False
        ) as file:
            file.write("# NetEase Cloud Music binding managed by xiaozhi-control-panel\n")
            temp_name = file.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.netease_env_path)

    @staticmethod
    def service_state(service):
        completed = subprocess.run(
            ["systemctl", "is-active", service], capture_output=True, text=True, check=False
        )
        return completed.stdout.strip() or "unknown"

    @staticmethod
    def restart_service(service):
        if service not in ALLOWED_SERVICES:
            raise ValueError("不允许操作该服务")
        subprocess.run(["systemctl", "restart", service], capture_output=True, text=True, check=True)

    def api_base_url(self):
        return self.get_music_config()["api_base_url"]

    async def netease_request(self, path, **params):
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{self.api_base_url()}/{path}", params=params) as response:
                payload = await response.json(content_type=None)
        if response.status >= 400 or not isinstance(payload, dict) or payload.get("code") != 200:
            raise RuntimeError("网易云二维码服务暂时不可用")
        return payload

    async def start_qr_login(self):
        key_payload = await self.netease_request("login/qr/key", timestamp=int(time.time() * 1000))
        unikey = key_payload.get("data", {}).get("unikey")
        if not unikey:
            raise RuntimeError("无法创建网易云二维码")
        qr_payload = await self.netease_request(
            "login/qr/create", key=unikey, qrimg="true", timestamp=int(time.time() * 1000)
        )
        qrimg = qr_payload.get("data", {}).get("qrimg")
        if not qrimg:
            raise RuntimeError("无法生成网易云二维码")
        self.qr_session = {"unikey": unikey, "expires_at": time.monotonic() + QR_TTL_SECONDS}
        return {"qrimg": qrimg, "expires_in": QR_TTL_SECONDS}

    async def poll_qr_login(self):
        if not self.qr_session or time.monotonic() >= self.qr_session["expires_at"]:
            self.qr_session = None
            return {"state": "expired"}
        payload = await self.netease_request(
            "login/qr/check", key=self.qr_session["unikey"], timestamp=int(time.time() * 1000)
        )
        code = payload.get("data", {}).get("code")
        if code == 803:
            cookie = payload.get("cookie") or payload.get("data", {}).get("cookie")
            if not cookie:
                raise RuntimeError("网易云未返回登录信息")
            self.save_netease_cookie(cookie)
            self.qr_session = None
            self.restart_service("xiaozhi-server")
            return {"state": "bound"}
        if code == 800:
            self.qr_session = None
            return {"state": "expired"}
        if code == 802:
            return {"state": "confirmed"}
        return {"state": "waiting"}


def json_response(data, status=200):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False), status=status, content_type="application/json"
    )


def create_app(panel):
    app = web.Application()

    async def index(_request):
        return web.FileResponse(Path(__file__).with_name("index.html"))

    async def status(_request):
        return json_response(
            {
                "services": {service: panel.service_state(service) for service in ALLOWED_SERVICES},
                "netease_bound": panel.is_netease_bound(),
                "music": panel.get_music_config(),
            }
        )

    async def get_music_config(_request):
        return json_response(panel.get_music_config())

    async def get_config(_request):
        return json_response({"yaml": panel.get_redacted_config_text()})

    async def put_config(request):
        try:
            panel.update_config_text((await request.json()).get("yaml"))
            panel.restart_service("xiaozhi-server")
            return json_response({"ok": True})
        except (ValueError, json.JSONDecodeError):
            return json_response({"error": "配置格式无效，未保存任何更改"}, 400)
        except subprocess.CalledProcessError:
            return json_response({"error": "配置已保存，但重启服务失败"}, 500)

    async def put_music_config(request):
        try:
            panel.update_music_config(await request.json())
            panel.restart_service("xiaozhi-server")
            return json_response({"ok": True})
        except (ValueError, json.JSONDecodeError) as error:
            return json_response({"error": str(error)}, 400)
        except subprocess.CalledProcessError:
            return json_response({"error": "配置已保存，但重启服务失败"}, 500)

    async def restart_service(request):
        try:
            panel.restart_service(request.match_info["service"])
            return json_response({"ok": True})
        except ValueError as error:
            return json_response({"error": str(error)}, 400)
        except subprocess.CalledProcessError:
            return json_response({"error": "服务重启失败"}, 500)

    async def start_qr(_request):
        try:
            return json_response(await panel.start_qr_login())
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
            return json_response({"error": "无法连接网易云二维码服务"}, 502)

    async def poll_qr(_request):
        try:
            return json_response(await panel.poll_qr_login())
        except (RuntimeError, ValueError) as error:
            return json_response({"error": str(error)}, 502)
        except (aiohttp.ClientError, asyncio.TimeoutError, subprocess.CalledProcessError):
            return json_response({"error": "绑定已保存，但服务重启失败"}, 500)

    async def unbind(_request):
        try:
            panel.clear_netease_cookie()
            panel.restart_service("xiaozhi-server")
            return json_response({"ok": True})
        except subprocess.CalledProcessError:
            return json_response({"error": "解绑已保存，但服务重启失败"}, 500)

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/music-config", get_music_config)
    app.router.add_put("/api/music-config", put_music_config)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_post("/api/services/{service}/restart", restart_service)
    app.router.add_post("/api/netease/qr/start", start_qr)
    app.router.add_post("/api/netease/qr/poll", poll_qr)
    app.router.add_post("/api/netease/unbind", unbind)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--config", default="/root/xiaozhi-esp32-server/main/xiaozhi-server/data/.config.yaml"
    )
    parser.add_argument("--netease-env", default="/etc/xiaozhi-server/netease.env")
    args = parser.parse_args()
    web.run_app(create_app(ControlPanel(args.config, args.netease_env)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
