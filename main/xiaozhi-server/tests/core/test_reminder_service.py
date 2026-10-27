import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.reminder_service import ReminderService


class _Server:
    def __init__(self):
        self.connection = None

    def get_reminder_connection(self):
        return self.connection


class _Connection:
    def __init__(self):
        self.messages = []

    async def deliver_reminder(self, message):
        self.messages.append(message)
        return True


class ReminderServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = _Server()
        self.service = ReminderService(
            {"reminders": {"database_path": str(Path(self.directory.name) / "reminders.sqlite3"), "poll_interval_seconds": 1}},
            self.server,
        )
        self.service._initialize_database()

    def tearDown(self):
        self.directory.cleanup()

    def test_once_reminder_is_persistent_and_completed_after_delivery(self):
        target = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=1)
        reminder = self.service.create_once("喝水", target.isoformat())
        self.assertEqual(reminder["status"], "active")
        claimed = self.service._claim_due()
        self.assertIsNone(claimed)

        with self.service._database() as connection:
            connection.execute("UPDATE reminders SET next_run_at = 0 WHERE id = ?", (reminder["id"],))
        claimed = self.service._claim_due()
        self.assertEqual(claimed["message"], "喝水")
        self.service._mark_delivered(claimed)
        self.assertEqual(self.service.get(reminder["id"])["status"], "completed")

    def test_daily_reminder_advances_after_delivery(self):
        reminder = self.service.create_daily("睡觉", "22:30")
        with self.service._database() as connection:
            connection.execute("UPDATE reminders SET next_run_at = 0 WHERE id = ?", (reminder["id"],))
        claimed = self.service._claim_due()
        self.service._mark_delivered(claimed)
        updated = self.service.get(reminder["id"])
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["time_of_day"], "22:30")

    def test_offline_device_retries_instead_of_losing_reminder(self):
        target = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=1)
        reminder = self.service.create_once("开会", target.isoformat())
        with self.service._database() as connection:
            connection.execute("UPDATE reminders SET next_run_at = 0 WHERE id = ?", (reminder["id"],))
        claimed = self.service._claim_due()
        self.service._retry(claimed)
        updated = self.service.get(reminder["id"])
        self.assertEqual(updated["status"], "active")
        self.assertNotEqual(updated["status"], "completed")
