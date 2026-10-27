from plugins_func.register import register_function, ToolType, ActionResponse, Action
from datetime import datetime


def _friendly_once_time(value: str) -> str:
    target = datetime.fromisoformat(value)
    now = datetime.now(target.tzinfo)
    day_offset = (target.date() - now.date()).days
    if day_offset == 0:
        day = "今天"
    elif day_offset == 1:
        day = "明天"
    else:
        day = f"{target.month}月{target.day}日"
    minute = f"{target.minute:02d}分" if target.minute else ""
    return f"{day}{target.hour}点{minute}"


def _friendly_daily_time(value: str) -> str:
    hour, minute = (int(part) for part in value.split(":", 1))
    return f"每天{hour}点{f'{minute:02d}分' if minute else ''}"


manage_reminder_function_desc = {
    "type": "function",
    "function": {
        "name": "manage_reminder",
        "description": "管理语音提醒。创建一次性提醒必须使用 create_once，并提供带时区的 ISO 8601 run_at；创建每日提醒必须使用 create_daily，并提供 24 小时制 time。查询使用 list；取消、暂停、恢复必须传 reminder_id。不要把时间或操作混在 message 中，也不要凭空声称已设置成功。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_once", "create_daily", "list", "cancel", "pause", "resume"],
                },
                "message": {"type": "string", "description": "提醒播报的内容，仅创建时需要"},
                "run_at": {"type": "string", "description": "一次性提醒的 ISO 8601 时间，例如 2026-10-27T21:30:00+08:00"},
                "time": {"type": "string", "description": "每日提醒的 HH:MM 时间，例如 22:30"},
                "reminder_id": {"type": "string", "description": "要取消、暂停或恢复的提醒 ID"},
            },
            "required": ["action"],
        },
    },
}


@register_function("manage_reminder", manage_reminder_function_desc, ToolType.SYSTEM_CTL)
async def manage_reminder(conn, action: str, message: str = "", run_at: str = "", time: str = "", reminder_id: str = ""):
    service = getattr(conn.server, "reminder_service", None)
    if service is None or not service.enabled:
        return ActionResponse(Action.RESPONSE, response="提醒服务当前未启用。")
    try:
        if action == "create_once":
            reminder = service.create_once(message, run_at)
            return ActionResponse(Action.RESPONSE, response=f"好的，我会在{_friendly_once_time(reminder['next_run_at'])}提醒你：{reminder['message']}。")
        if action == "create_daily":
            reminder = service.create_daily(message, time)
            return ActionResponse(Action.RESPONSE, response=f"好的，我会{_friendly_daily_time(reminder['time_of_day'])}提醒你：{reminder['message']}。")
        if action == "list":
            reminders = service.list()
            if not reminders:
                return ActionResponse(Action.RESPONSE, response="当前没有待执行的提醒。")
            text = "；".join(
                f"{_friendly_daily_time(item['time_of_day']) if item['schedule_type'] == 'daily' else _friendly_once_time(item['next_run_at'])}，提醒{item['message']}"
                for item in reminders[:5]
            )
            return ActionResponse(Action.RESPONSE, response=f"当前提醒有：{text}。")
        if action == "cancel":
            ok = service.cancel(reminder_id)
            return ActionResponse(Action.RESPONSE, response="已取消该提醒。" if ok else "没有找到可取消的提醒。")
        if action in {"pause", "resume"}:
            ok = service.set_status(reminder_id, "paused" if action == "pause" else "active")
            return ActionResponse(Action.RESPONSE, response=("已暂停该提醒。" if action == "pause" else "已恢复该提醒。") if ok else "没有找到可操作的提醒。")
        return ActionResponse(Action.RESPONSE, response="不支持的提醒操作。")
    except ValueError as exc:
        return ActionResponse(Action.RESPONSE, response=f"提醒没有设置成功：{exc}。")
