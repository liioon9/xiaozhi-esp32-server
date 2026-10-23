from typing import TYPE_CHECKING, Any, Dict

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


class MediaTextMessageHandler(TextMessageHandler):
    """处理客户端独立音乐播放器的状态事件。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MEDIA

    async def handle(
        self, conn: "ConnectionHandler", msg_json: Dict[str, Any]
    ) -> None:
        if msg_json.get("action") != "ended":
            return

        from plugins_func.functions.play_music import play_next_netease_music

        if await play_next_netease_music(conn):
            conn.logger.bind(tag=__name__).info("当前歌曲播放结束，已下发下一首")
        else:
            conn.logger.bind(tag=__name__).info("播放队列已结束")
