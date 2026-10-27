"""Persistent, single-device reminder scheduling.

The service deliberately uses only the Python standard library.  Reminder
delivery is retried until the physical device has its standby WebSocket open;
the text is persisted before it is spoken, so no LLM call is needed at the
scheduled time.
"""

import asyncio
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class ReminderService:
    def __init__(self, config: dict, websocket_server):
        reminder_config = config.get("reminders", {})
        self.enabled = reminder_config.get("enabled", True)
        self.timezone_name = reminder_config.get("timezone", "Asia/Shanghai")
        self.poll_interval_seconds = int(reminder_config.get("poll_interval_seconds", 5))
        database_path = reminder_config.get("database_path", "data/reminders.sqlite3")
        self.database_path = Path(database_path)
        self.websocket_server = websocket_server
        self._task = None
        self._stopping = asyncio.Event()

    def _connect(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _database(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self):
        with self._database() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    schedule_type TEXT NOT NULL CHECK(schedule_type IN ('once', 'daily')),
                    timezone TEXT NOT NULL,
                    time_of_day TEXT,
                    next_run_at INTEGER NOT NULL,
                    last_run_at INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'dispatching', 'completed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            # A restart must not leave a reminder permanently claimed.
            connection.execute(
                "UPDATE reminders SET status = 'active', updated_at = ? WHERE status = 'dispatching'",
                (int(time.time()),),
            )

    @staticmethod
    def _row_to_dict(row):
        item = dict(row)
        for field in ("next_run_at", "last_run_at", "created_at", "updated_at"):
            if item.get(field) is not None:
                item[field] = datetime.fromtimestamp(item[field], ZoneInfo(item["timezone"])).isoformat()
        return item

    def create_once(self, message: str, run_at: str) -> dict:
        if not message or not message.strip():
            raise ValueError("提醒内容不能为空")
        try:
            target = datetime.fromisoformat(run_at)
        except ValueError as exc:
            raise ValueError("run_at 必须是 ISO 8601 时间，例如 2026-10-27T21:30:00+08:00") from exc
        timezone = ZoneInfo(self.timezone_name)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone)
        target = target.astimezone(timezone)
        if target <= datetime.now(timezone):
            raise ValueError("一次性提醒时间必须在当前时间之后")
        return self._insert(message.strip(), "once", target, None)

    def create_daily(self, message: str, time_of_day: str) -> dict:
        if not message or not message.strip():
            raise ValueError("提醒内容不能为空")
        try:
            hour, minute = (int(part) for part in time_of_day.split(":", 1))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except ValueError as exc:
            raise ValueError("time 必须是 24 小时制 HH:MM，例如 22:30") from exc
        timezone = ZoneInfo(self.timezone_name)
        now = datetime.now(timezone)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return self._insert(message.strip(), "daily", target, f"{hour:02d}:{minute:02d}")

    def _insert(self, message, schedule_type, target, time_of_day):
        now = int(time.time())
        reminder_id = uuid.uuid4().hex[:12]
        with self._database() as connection:
            connection.execute(
                """INSERT INTO reminders
                   (id, message, schedule_type, timezone, time_of_day, next_run_at, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (reminder_id, message, schedule_type, self.timezone_name, time_of_day,
                 int(target.timestamp()), now, now),
            )
        return self.get(reminder_id)

    def get(self, reminder_id: str):
        with self._database() as connection:
            row = connection.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, include_completed=False):
        where = "" if include_completed else "WHERE status != 'completed'"
        with self._database() as connection:
            rows = connection.execute(
                f"SELECT * FROM reminders {where} ORDER BY next_run_at ASC"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def set_status(self, reminder_id: str, status: str):
        if status not in {"active", "paused"}:
            raise ValueError("状态只能是 active 或 paused")
        with self._database() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ? AND status != 'completed'",
                (status, int(time.time()), reminder_id),
            )
        return cursor.rowcount > 0

    def cancel(self, reminder_id: str):
        with self._database() as connection:
            cursor = connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return cursor.rowcount > 0

    def _claim_due(self):
        now = int(time.time())
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM reminders
                   WHERE status = 'active' AND next_run_at <= ?
                   ORDER BY next_run_at ASC LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE reminders SET status = 'dispatching', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            return self._row_to_dict(row)

    def _mark_delivered(self, reminder):
        now = int(time.time())
        with self._database() as connection:
            if reminder["schedule_type"] == "once":
                connection.execute(
                    "UPDATE reminders SET status = 'completed', last_run_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, reminder["id"]),
                )
                return
            timezone = ZoneInfo(reminder["timezone"])
            hour, minute = (int(part) for part in reminder["time_of_day"].split(":", 1))
            next_run = datetime.now(timezone).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= datetime.now(timezone):
                next_run += timedelta(days=1)
            connection.execute(
                """UPDATE reminders SET status = 'active', last_run_at = ?, next_run_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, int(next_run.timestamp()), now, reminder["id"]),
            )

    def _retry(self, reminder):
        now = int(time.time())
        with self._database() as connection:
            connection.execute(
                "UPDATE reminders SET status = 'active', next_run_at = ?, updated_at = ? WHERE id = ?",
                (now + 30, now, reminder["id"]),
            )

    async def start(self):
        if not self.enabled:
            return
        await asyncio.to_thread(self._initialize_database)
        self._task = asyncio.create_task(self._run(), name="reminder-scheduler")

    async def stop(self):
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self):
        while not self._stopping.is_set():
            reminder = await asyncio.to_thread(self._claim_due)
            if reminder is None:
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            connection = self.websocket_server.get_reminder_connection()
            try:
                delivered = connection is not None and await connection.deliver_reminder(reminder["message"])
            except Exception:
                delivered = False
            if delivered:
                await asyncio.to_thread(self._mark_delivered, reminder)
            else:
                await asyncio.to_thread(self._retry, reminder)
