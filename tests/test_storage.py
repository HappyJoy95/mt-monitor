import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.mt_monitor.storage import save_snapshot


class SaveSnapshotTests(unittest.TestCase):
    def test_save_snapshot_writes_raw_and_latest_summary(self):
        payload = {"data": {"name": "测试"}}
        summary = [{"order_id": "123"}]

        with TemporaryDirectory() as directory:
            raw_path, summary_path = save_snapshot(
                Path(directory), payload, summary
            )

            self.assertEqual(raw_path.parent.name, "raw")
            self.assertEqual(raw_path.name[-16:], "-order-list.json")
            raw_text = raw_path.read_text(encoding="utf-8")
            self.assertIn("\n  ", raw_text)
            self.assertIn("测试", raw_text)
            self.assertEqual(json.loads(raw_text), payload)
            self.assertEqual(summary_path, Path(directory) / "data/latest-orders.json")
            self.assertEqual(
                json.loads(summary_path.read_text(encoding="utf-8")), summary
            )
