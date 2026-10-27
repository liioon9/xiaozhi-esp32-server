import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from plugins_func.functions.manage_reminder import _friendly_daily_time, _friendly_once_time


class ReminderTextTest(unittest.TestCase):
    def test_daily_time_is_spoken_in_chinese(self):
        self.assertEqual(_friendly_daily_time("22:30"), "每天22点30分")
        self.assertEqual(_friendly_daily_time("07:00"), "每天7点")

    def test_future_time_does_not_expose_iso_timestamp(self):
        target = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)
        text = _friendly_once_time(target.isoformat())
        self.assertIn(f"{target.month}月{target.day}日", text)
        self.assertNotIn("T", text)
        self.assertNotIn("+08:00", text)
