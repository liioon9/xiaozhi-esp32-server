import os
import re
import time
import random
import difflib
import traceback
import json
from pathlib import Path
from core.api.music_handler import (
    build_track_catalog,
    ensure_cached_ogg,
    get_netease_track_stream_url,
    get_track_id,
    get_track_stream_url,
    register_netease_track,
)
from core.providers.music.netease_cli import (
    NeteaseCliError,
    first_playable,
)
from core.providers.music.netease_provider import create_netease_provider
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

MUSIC_CACHE = {}

play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": "开始一个新的音乐播放队列。必须用 mode 明确选择操作，不能把操作类型写进 query：指定歌曲用 song，播放某艺人的歌曲用 artist，每日推荐用 daily，心动模式用 heartbeat，红心收藏用 favorite。用户说随机播放、顺序播放、循环播放、单曲循环时，必须调用 control_music，不能调用本工具。song 和 artist 必须提供 query。",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": [
                        "song",
                        "artist",
                        "daily",
                        "heartbeat",
                        "favorite",
                    ],
                    "description": "明确的播放操作类型。",
                },
                "query": {
                    "type": "string",
                    "description": "mode=song 时填歌曲名；mode=artist 时只填艺人名。其他模式省略。",
                },
            },
            "required": ["mode"],
        },
    },
}

control_music_function_desc = {
    "type": "function",
    "function": {
        "name": "control_music",
        "description": "当用户要求控制当前音乐播放时调用。用户说随机播放、顺序播放、列表循环、单曲循环时，必须调用本工具以切换当前队列的播放模式，而不是重新调用 play_music。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "pause",
                        "resume",
                        "stop",
                        "previous",
                        "next",
                        "sequence",
                        "repeat_all",
                        "repeat_one",
                        "shuffle",
                    "favorite",
                    "unfavorite",
                    ],
            "description": "暂停为 pause，继续或恢复为 resume，停止为 stop，上一首为 previous，下一首、换一首或切歌为 next。顺序播放为 sequence，列表循环为 repeat_all，单曲循环为 repeat_one，随机播放或开启随机为 shuffle。收藏当前歌曲为 favorite，取消收藏为 unfavorite。",
                }
            },
            "required": ["action"],
        },
    },
}


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
async def play_music(conn: "ConnectionHandler", mode: str, query: str = ""):
    try:
        if mode not in {
            "song",
            "artist",
            "daily",
            "heartbeat",
            "favorite",
        }:
            raise NeteaseCliError("不支持的音乐播放模式")
        if mode in {"song", "artist"} and not query.strip():
            raise NeteaseCliError("请提供歌曲名或艺人名")

        initialize_music_handler(conn)
        music_source = MUSIC_CACHE.get("music_config", {}).get("source", "local")
        if music_source == "netease":
            await play_netease_music(conn, mode, query.strip())
        elif mode == "song":
            await handle_music_command(conn, f"播放音乐 {query.strip()}")
        else:
            raise NeteaseCliError("当前音乐源不支持该播放模式")
        return ActionResponse(
            action=Action.RECORD, result="指令已接收", response="正在为您播放音乐"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"处理音乐意图错误: {e}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(e), response="播放音乐时出错了"
        )


@register_function("control_music", control_music_function_desc, ToolType.SYSTEM_CTL)
async def control_music(conn: "ConnectionHandler", action: str):
    if action not in {
        "pause",
        "resume",
        "stop",
        "previous",
        "next",
        "sequence",
        "repeat_all",
        "repeat_one",
        "shuffle",
        "favorite",
        "unfavorite",
    }:
        return ActionResponse(
            action=Action.RESPONSE, result="无效音乐控制指令", response="不支持该音乐控制操作"
        )

    if action == "next":
        if not await play_next_netease_music(conn):
            return ActionResponse(
                action=Action.RESPONSE,
                result="播放队列已结束",
                response="当前播放列表已经没有下一首了。",
            )
        conn.logger.bind(tag=TAG).info("已下发独立音乐控制命令: next")
        return ActionResponse(
            action=Action.RECORD, result="指令已接收", response="好的，正在播放下一首。"
        )

    if action == "previous":
        if not await play_previous_netease_music(conn):
            return ActionResponse(
                action=Action.RESPONSE,
                result="没有上一首",
                response="当前已经是播放列表的第一首了。",
            )
        conn.logger.bind(tag=TAG).info("已下发独立音乐控制命令: previous")
        return ActionResponse(
            action=Action.RECORD, result="指令已接收", response="好的，正在播放上一首。"
        )

    if action in {"sequence", "repeat_all", "repeat_one", "shuffle"}:
        conn.music_play_mode = action
        response = {
            "sequence": "好的，已切换为顺序播放。",
            "repeat_all": "好的，已开启列表循环。",
            "repeat_one": "好的，已开启单曲循环。",
            "shuffle": "好的，已开启随机播放。",
        }[action]
        conn.logger.bind(tag=TAG).info(f"已切换音乐播放模式: {action}")
        return ActionResponse(action=Action.RECORD, result="指令已接收", response=response)

    if action in {"favorite", "unfavorite"}:
        queue = getattr(conn, "music_queue", [])
        index = getattr(conn, "music_queue_index", -1)
        if not queue or index < 0 or index >= len(queue):
            return ActionResponse(
                action=Action.RESPONSE,
                result="没有正在播放的网易云歌曲",
                response="当前没有可收藏的网易云歌曲。",
            )
        provider = MUSIC_CACHE.get("netease_provider")
        if provider is None:
            provider = create_netease_provider(conn.config)
            MUSIC_CACHE["netease_provider"] = provider
        track = queue[index]
        if action == "favorite":
            await provider.like_track(track)
            response = "好的，已收藏当前歌曲。"
        else:
            await provider.dislike_track(track)
            response = "好的，已取消收藏当前歌曲。"
        conn.logger.bind(tag=TAG).info(f"已更新当前歌曲收藏状态: {action}")
        return ActionResponse(action=Action.RECORD, result="指令已接收", response=response)

    await conn.websocket.send(json.dumps({"type": "media", "action": action}, ensure_ascii=False))
    if action == "stop":
        conn.music_queue = []
        conn.music_queue_index = -1
        conn.music_play_history = []
    response = {
        "pause": "好的，音乐暂停了。",
        "resume": "好的，继续播放音乐。",
        "stop": "好的，已停止播放音乐。",
    }[action]
    conn.logger.bind(tag=TAG).info(f"已下发独立音乐控制命令: {action}")
    return ActionResponse(action=Action.RECORD, result="指令已接收", response=response)


def _extract_song_name(text):
    """从用户输入中提取歌名"""
    for keyword in ["播放音乐"]:
        if keyword in text:
            parts = text.split(keyword)
            if len(parts) > 1:
                return parts[1].strip()
    return None


def _find_best_match(potential_song, music_files):
    """查找最匹配的歌曲"""
    best_match = None
    highest_ratio = 0

    for music_file in music_files:
        song_name = os.path.splitext(music_file)[0]
        ratio = difflib.SequenceMatcher(None, potential_song, song_name).ratio()
        if ratio > highest_ratio and ratio > 0.4:
            highest_ratio = ratio
            best_match = music_file
    return best_match


def get_music_files(music_dir, music_ext):
    music_dir = Path(music_dir)
    music_files = []
    music_file_names = []
    for file in music_dir.rglob("*"):
        # 判断是否是文件
        if file.is_file():
            # 获取文件扩展名
            ext = file.suffix.lower()
            # 判断扩展名是否在列表中
            if ext in music_ext:
                # 添加相对路径
                music_files.append(str(file.relative_to(music_dir)))
                music_file_names.append(
                    os.path.splitext(str(file.relative_to(music_dir)))[0]
                )
    return music_files, music_file_names


def initialize_music_handler(conn: "ConnectionHandler"):
    global MUSIC_CACHE
    if MUSIC_CACHE == {}:
        plugins_config = conn.config.get("plugins", {})
        if "play_music" in plugins_config:
            MUSIC_CACHE["music_config"] = plugins_config["play_music"]
            MUSIC_CACHE["music_dir"] = os.path.abspath(
                MUSIC_CACHE["music_config"].get("music_dir", "./music")  # 默认路径修改
            )
            MUSIC_CACHE["music_ext"] = MUSIC_CACHE["music_config"].get(
                "music_ext", (".mp3", ".wav", ".p3")
            )
            MUSIC_CACHE["refresh_time"] = MUSIC_CACHE["music_config"].get(
                "refresh_time", 60
            )
        else:
            MUSIC_CACHE["music_dir"] = os.path.abspath("./music")
            MUSIC_CACHE["music_ext"] = (".mp3", ".wav", ".p3")
            MUSIC_CACHE["refresh_time"] = 60
        # 获取音乐文件列表
        MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = get_music_files(
            MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"]
        )
        MUSIC_CACHE["scan_time"] = time.time()
    return MUSIC_CACHE


async def handle_music_command(conn: "ConnectionHandler", text):
    """Handle a local-file music request."""
    initialize_music_handler(conn)
    global MUSIC_CACHE

    clean_text = re.sub(r"[^\w\s]", "", text).strip()
    conn.logger.bind(tag=TAG).debug(f"检查是否是音乐命令: {clean_text}")

    # 尝试匹配具体歌名
    if os.path.exists(MUSIC_CACHE["music_dir"]):
        if time.time() - MUSIC_CACHE["scan_time"] > MUSIC_CACHE["refresh_time"]:
            # 刷新音乐文件列表
            MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = (
                get_music_files(MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"])
            )
            MUSIC_CACHE["scan_time"] = time.time()

        potential_song = _extract_song_name(clean_text)
        if potential_song:
            best_match = _find_best_match(potential_song, MUSIC_CACHE["music_files"])
            if best_match:
                conn.logger.bind(tag=TAG).info(f"找到最匹配的歌曲: {best_match}")
                await play_local_music(conn, specific_file=best_match)
                return True
    # 检查是否是通用播放音乐命令
    await play_local_music(conn)
    return True


async def play_netease_music(conn: "ConnectionHandler", mode: str, query: str = ""):
    provider = MUSIC_CACHE.get("netease_provider")
    if provider is None:
        provider = create_netease_provider(conn.config)
        MUSIC_CACHE["netease_provider"] = provider

    if mode == "daily":
        tracks = await provider.daily()
    elif mode == "favorite":
        tracks = await provider.favorite_tracks()
    elif mode == "heartbeat":
        playlist_id = await provider.favorite_playlist_id()
        queue = getattr(conn, "music_queue", [])
        current_index = getattr(conn, "music_queue_index", -1)
        current_track = (
            queue[current_index]
            if 0 <= current_index < len(queue)
            else None
        )
        if current_track is None:
            current_track = first_playable(await provider.favorite_tracks())
        if current_track is None or not current_track.get("id"):
            raise NeteaseCliError("需要一首可用歌曲才能启动心动模式")
        tracks = await provider.heartbeat(playlist_id, current_track["id"])
    elif mode == "artist":
        tracks = await provider.search_artist_tracks(query, limit=30)
        if not tracks:
            raise NeteaseCliError(f"没有找到歌手：{query}")
    elif mode == "song":
        if not query:
            raise NeteaseCliError("请告诉我想播放的歌曲名称")
        tracks = await provider.search(query)
    else:
        raise NeteaseCliError("不支持的网易云音乐播放模式")

    queue = [track for track in tracks if first_playable([track])]
    track = queue[0] if queue else None
    if track is None:
        raise NeteaseCliError("没有找到当前账号可播放的歌曲")

    conn.music_queue = queue
    conn.music_queue_index = 0
    conn.music_play_history = [0]
    await _play_netease_track(conn, track)


async def play_next_netease_music(conn: "ConnectionHandler"):
    queue = getattr(conn, "music_queue", [])
    current_index = getattr(conn, "music_queue_index", -1)
    if not queue or current_index < 0:
        return False

    mode = getattr(conn, "music_play_mode", "sequence")
    if mode == "repeat_one":
        next_index = current_index
    elif mode == "shuffle":
        candidates = [index for index in range(len(queue)) if index != current_index]
        next_index = random.choice(candidates) if candidates else current_index
    elif current_index + 1 < len(queue):
        next_index = current_index + 1
    elif mode == "repeat_all":
        next_index = 0
    else:
        return False

    await _play_netease_queue_index(conn, next_index)
    return True


async def play_previous_netease_music(conn: "ConnectionHandler"):
    queue = getattr(conn, "music_queue", [])
    history = getattr(conn, "music_play_history", [])
    if not queue:
        return False

    if len(history) < 2:
        if getattr(conn, "music_play_mode", "sequence") != "repeat_all":
            return False
        await _play_netease_queue_index(conn, len(queue) - 1)
        return True

    history.pop()
    await _play_netease_queue_index(conn, history[-1], append_history=False)
    return True


async def _play_netease_queue_index(conn: "ConnectionHandler", index: int, append_history=True):
    conn.music_queue_index = index
    if append_history:
        history = getattr(conn, "music_play_history", [])
        if not history or history[-1] != index:
            history.append(index)
        conn.music_play_history = history
    await _play_netease_track(conn, conn.music_queue[index])


async def _play_netease_track(conn: "ConnectionHandler", track):
    token = register_netease_track(conn.config, track)
    stream_url = get_netease_track_stream_url(conn.config, token)
    artist_names = "、".join(
        artist.get("name", "") for artist in track.get("artists", []) if artist.get("name")
    )
    title = track.get("name", "网易云音乐")
    display_title = f"{title} - {artist_names}" if artist_names else title
    await _send_music_playback(conn, token, display_title, stream_url)


def _get_random_play_prompt(song_name):
    """生成随机播放引导语"""
    # 移除文件扩展名
    clean_name = os.path.splitext(song_name)[0]
    prompts = [
        f"正在为您播放，《{clean_name}》",
        f"请欣赏歌曲，《{clean_name}》",
        f"即将为您播放，《{clean_name}》",
        f"现在为您带来，《{clean_name}》",
        f"让我们一起聆听，《{clean_name}》",
        f"接下来请欣赏，《{clean_name}》",
        f"此刻为您献上，《{clean_name}》",
    ]
    # 直接使用random.choice，不设置seed
    return random.choice(prompts)


async def play_local_music(conn: "ConnectionHandler", specific_file=None):
    global MUSIC_CACHE
    """播放本地音乐文件"""
    try:
        if not os.path.exists(MUSIC_CACHE["music_dir"]):
            raise FileNotFoundError(f"音乐目录不存在: {MUSIC_CACHE['music_dir']}")

        # 确保路径正确性
        if specific_file:
            selected_music = specific_file
            music_path = os.path.join(MUSIC_CACHE["music_dir"], specific_file)
        else:
            if not MUSIC_CACHE["music_files"]:
                raise FileNotFoundError("音乐目录中没有找到支持的音乐文件")
            selected_music = random.choice(MUSIC_CACHE["music_files"])
            music_path = os.path.join(MUSIC_CACHE["music_dir"], selected_music)

        if not os.path.exists(music_path):
            conn.logger.bind(tag=TAG).error(f"选定的音乐文件不存在: {music_path}")
            return
        relative_path = Path(selected_music).as_posix()
        track_id = get_track_id(relative_path)
        track = build_track_catalog(conn.config).get(track_id)
        if track is None:
            raise RuntimeError("选定的音乐不在可播放目录中")

        # Convert before sending the command so the ESP32 HTTP request gets a response
        # immediately instead of timing out while ffmpeg prepares the complete file.
        await ensure_cached_ogg(conn.config, track)
        stream_url = get_track_stream_url(conn.config, track_id)

        await _send_music_playback(conn, track_id, track["title"], stream_url)

    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"播放音乐失败: {str(e)}")
        conn.logger.bind(tag=TAG).error(f"详细错误: {traceback.format_exc()}")
        raise


async def _send_music_playback(conn, track_id, title, stream_url):
    text = _get_random_play_prompt(title)
    conn.tts.store_tts_text(conn.sentence_id, text)

    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    await conn.websocket.send(
        json.dumps(
            {
                "type": "media",
                "action": "play",
                "track": {
                    "id": track_id,
                    "title": title,
                    "url": stream_url,
                    "format": "ogg-opus",
                },
            },
            ensure_ascii=False,
        )
    )
    conn.logger.bind(tag=TAG).info(f"已下发独立音乐播放命令: {title}")
