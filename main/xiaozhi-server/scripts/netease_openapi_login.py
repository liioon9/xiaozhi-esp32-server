"""One-time interactive login for the 网易云开放平台 music provider.

Run this on the server after NETEASE_APP_ID, NETEASE_APP_SECRET and
NETEASE_PRIVATE_KEY_PATH are configured. It prints a short-lived QR URL; scan
it in the official NetEase Cloud Music app. No credential is printed.
"""

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.providers.music.netease_openapi import (  # noqa: E402
    NeteaseOpenApiError,
    NeteaseOpenApiProvider,
)


async def main():
    provider = NeteaseOpenApiProvider(
        {
            "plugins": {
                "play_music": {
                    "netease": {
                        "token_store": "/root/.config/xiaozhi/netease_openapi_tokens.json",
                        "device": {
                            "channel": "${NETEASE_DEVICE_CHANNEL}",
                            "deviceId": "${NETEASE_DEVICE_ID}",
                            "deviceType": "${NETEASE_DEVICE_TYPE}",
                            "appVer": "${NETEASE_DEVICE_APP_VER}",
                            "os": "${NETEASE_DEVICE_OS}",
                            "osVer": "${NETEASE_DEVICE_OS_VER}",
                            "brand": "${NETEASE_DEVICE_BRAND}",
                            "model": "${NETEASE_DEVICE_MODEL}",
                            "clientIp": "${NETEASE_DEVICE_CLIENT_IP}",
                            "netStatus": "${NETEASE_DEVICE_NET_STATUS}",
                        },
                    }
                }
            }
        }
    )
    anonymous_token, qr = await provider.create_qr_login()
    print("请在 5 分钟内用网易云音乐 App 打开并扫码：")
    print(qr["qrCodeUrl"])

    for _ in range(150):
        await asyncio.sleep(2)
        status = await provider.poll_qr_login(anonymous_token, qr["uniKey"])
        state = status.get("status")
        if state == 803:
            print("登录成功，令牌已安全保存。")
            return
        if state == 800:
            raise NeteaseOpenApiError("二维码已过期，请重新运行脚本")
    raise NeteaseOpenApiError("二维码登录超时，请重新运行脚本")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except NeteaseOpenApiError as error:
        print(f"登录失败：{error}", file=sys.stderr)
        raise SystemExit(1)
