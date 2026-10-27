from core.providers.music.netease_api_enhanced import NeteaseApiEnhancedProvider
from core.providers.music.netease_cli import NeteaseCliError, NeteaseCliProvider


def create_netease_provider(config):
    music_config = config.get("plugins", {}).get("play_music", {})
    netease_config = music_config.get("netease", {})
    provider = str(netease_config.get("provider", "cli")).lower()
    if provider == "api_enhanced":
        return NeteaseApiEnhancedProvider(config)
    if provider == "cli":
        return NeteaseCliProvider(config)
    raise NeteaseCliError(f"不支持的网易云音乐提供方: {provider}")
