import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.issue_time_ai import determine_issue_time_frames
from utils.issue_time_utils import (
    format_issue_time,
    read_log_time_range,
    validate_issue_time_in_log_range,
)


class IssueTimeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        capture = Path(self.tmp.name) / "HOST_12-01-2026_11-13-43_468_9_6050"
        capture.mkdir()
        (capture / "system_info.txt").write_text(
            json.dumps({"System Time Zone": "中国标准时间 (GMT+0800)"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.log_path = capture / "WifiDriverIHVSession.etl.002.log"
        self.log_path.write_text(
            "01/11/2026-19:27:48.513 [19] first\n"
            "01/12/2026-11:13:50.032 [11] last\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_wrong_date_outside_selected_log(self):
        issue_dt, first_ts, last_ts, error = validate_issue_time_in_log_range(
            "01/11/2026-11:02:29.000", str(self.log_path)
        )
        self.assertIsNone(issue_dt)
        self.assertEqual(datetime(2026, 1, 11, 19, 27, 48, 513000), first_ts)
        self.assertEqual(datetime(2026, 1, 12, 11, 13, 50, 32000), last_ts)
        self.assertIn("outside", error)

    def test_time_only_uses_the_only_date_inside_cross_midnight_log(self):
        issue_dt, _, _, error = validate_issue_time_in_log_range(
            "11:02:29", str(self.log_path)
        )
        self.assertEqual("", error)
        self.assertEqual("01/12/2026-11:02:29.000", format_issue_time(issue_dt))

    def test_gmt8_customer_and_decode_host_are_a_no_op(self):
        issue_dt = datetime(2026, 1, 12, 11, 2, 29)
        frames = determine_issue_time_frames(
            issue_dt,
            [str(self.log_path)],
            *read_log_time_range(str(self.log_path)),
        )
        self.assertEqual(issue_dt, frames["log_frame"])
        self.assertEqual(issue_dt, frames["customer_frame"])
        self.assertEqual("same_timezone", frames["source_frame"])

    def test_time_only_rejects_ambiguous_multi_day_range(self):
        self.log_path.write_text(
            "01/11/2026-09:00:00.000 first\n"
            "01/13/2026-12:00:00.000 last\n",
            encoding="utf-8",
        )
        issue_dt, _, _, error = validate_issue_time_in_log_range(
            "11:02:29", str(self.log_path)
        )
        self.assertIsNone(issue_dt)
        self.assertIn("more than one date", error)

    def test_rejects_when_log_range_cannot_be_read(self):
        self.log_path.write_text("no timestamp here\n", encoding="utf-8")
        issue_dt, first_ts, last_ts, error = validate_issue_time_in_log_range(
            "01/12/2026-11:02:29.000", str(self.log_path)
        )
        self.assertIsNone(issue_dt)
        self.assertIsNone(first_ts)
        self.assertIsNone(last_ts)
        self.assertIn("no readable date range", error)


if __name__ == "__main__":
    unittest.main()
