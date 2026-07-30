import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from src.mt_monitor.cli import main


class CliTests(unittest.TestCase):
    def test_import_writes_raw_response_and_summary(self):
        payload = {
            "data": {
                "orderList": [{
                    "commonInfo": '{"wm_order_id_view": "123"}',
                    "orderInfo": '{"chargeInfo": {"userPayTotalAmount": 210.0}, "unifiedBasicInfo": {"wmPoiName": "测试门店", "orderStatusDesc": "待接单"}, "foodInfo": {"cartDetails": []}}',
                }]
            }
        }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "response.json"
            source.write_text(json.dumps(payload), encoding="utf-8")

            exit_code = main(["import", str(source), "--root", str(root)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads((root / "data/latest-orders.json").read_text(encoding="utf-8")),
                [{
                    "order_id": "123",
                    "status": "待接单",
                    "store": "测试门店",
                    "user_paid": 210.0,
                    "items": [],
                }],
            )
            self.assertEqual(len(list((root / "raw").glob("*-order-list.json"))), 1)

    def test_import_rejects_a_non_order_payload_without_writing_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "response.json"
            source.write_text('{"data": {}}', encoding="utf-8")

            exit_code = main(["import", str(source), "--root", str(root)])

            self.assertEqual(exit_code, 2)
            self.assertFalse((root / "raw").exists())
            self.assertFalse((root / "data").exists())


class PullDependencyTest(unittest.TestCase):
    def test_pull_without_playwright_guides_install(self):
        # Simulate a missing playwright module; the CLI should emit the install
        # hint and exit 3, NOT a generic "拉取失败：No module named...".
        with mock.patch.dict(
            sys.modules, {"playwright": None, "playwright.sync_api": None}
        ):
            with TemporaryDirectory() as directory:
                err = io.StringIO()
                with redirect_stderr(err):
                    exit_code = main(
                        ["pull", "--root", str(Path(directory)), "--no-notify"]
                    )
        self.assertEqual(exit_code, 3)
        out = err.getvalue()
        self.assertIn("playwright", out)
        self.assertNotIn("拉取失败", out)
