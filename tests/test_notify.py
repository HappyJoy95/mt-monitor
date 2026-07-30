import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.mt_monitor import notify
from src.mt_monitor.wechat_webhook import WechatWebhookClient

VALID_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=ABC123"


class FormatTest(unittest.TestCase):
    def test_contains_key_fields(self):
        order = {
            "order_id": "2602234390179434618",
            "status": "待接单",
            "store": "华为授权体验店（新业广场店）",
            "items": [{"name": "华为冰糖全能充电器", "quantity": 1}],
        }
        text = notify.format_notification(order, order["status"])
        self.assertIn("2602234390179434618", text)
        self.assertIn("待接单", text)
        self.assertIn("华为授权体验店（新业广场店）", text)
        self.assertIn("华为冰糖全能充电器", text)


class DryRunTest(unittest.TestCase):
    def test_dry_run_counts_without_webhook(self):
        orders = [{"order_id": "1", "status": "待接单", "store": "S"}]
        pushed, skipped = notify.process_notifications(
            orders, Path("/nonexistent/notify"), root=".", dry_run=True
        )
        self.assertEqual((pushed, skipped), (1, 0))


class MissingWebhookTest(unittest.TestCase):
    def test_missing_file_returns_zero(self):
        orders = [{"order_id": "1", "status": "待接单", "store": "S"}]
        pushed, skipped = notify.process_notifications(
            orders, Path("/nonexistent/notify"), root="."
        )
        self.assertEqual((pushed, skipped), (0, 0))


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_notify_test"
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / "config").mkdir(exist_ok=True)
        (self.tmp / "config" / "notify").write_text(VALID_URL, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dedup_skips_already_pushed(self):
        orders = [{"order_id": "ORD1", "status": "待接单", "store": "S"}]
        with mock.patch.object(
            WechatWebhookClient, "send_text", return_value=None
        ) as m:
            p1, s1 = notify.process_notifications(
                orders, self.tmp / "config" / "notify", root=self.tmp, dedup=True
            )
            p2, s2 = notify.process_notifications(
                orders, self.tmp / "config" / "notify", root=self.tmp, dedup=True
            )
        self.assertEqual((p1, s1), (1, 0))
        self.assertEqual((p2, s2), (0, 1))
        self.assertEqual(m.call_count, 1)


class InvalidConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_notify_invalid"
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / "config").mkdir(exist_ok=True)
        (self.tmp / "config" / "notify").write_text(
            "https://example.com/not/a/webhook", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_invalid_url_skips(self):
        orders = [{"order_id": "1", "status": "待接单", "store": "S"}]
        pushed, skipped = notify.process_notifications(
            orders, self.tmp / "config" / "notify", root=self.tmp
        )
        self.assertEqual((pushed, skipped), (0, 0))


class EnvVarTest(unittest.TestCase):
    def test_env_var_works_without_config_file(self):
        # Only the env var is set; no config/notify file exists.
        orders = [{"order_id": "1", "status": "待接单", "store": "S"}]
        with mock.patch.dict(os.environ, {"QYWECHAT_WEBHOOK": VALID_URL}):
            with mock.patch.object(
                WechatWebhookClient, "send_text", return_value=None
            ) as m:
                pushed, skipped = notify.process_notifications(
                    orders, Path("/nonexistent/notify"), root="."
                )
        self.assertEqual((pushed, skipped), (1, 0))
        self.assertEqual(m.call_count, 1)

    def test_env_var_takes_precedence_over_file(self):
        # File holds an INVALID url, but the env var is valid -> env wins.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            notify_file = root / "config" / "notify"
            notify_file.parent.mkdir(parents=True, exist_ok=True)
            notify_file.write_text(
                "https://example.com/not/a/webhook", encoding="utf-8"
            )
            orders = [{"order_id": "1", "status": "待接单", "store": "S"}]
            with mock.patch.dict(os.environ, {"QYWECHAT_WEBHOOK": VALID_URL}):
                with mock.patch.object(
                    WechatWebhookClient, "send_text", return_value=None
                ) as m:
                    pushed, skipped = notify.process_notifications(
                        orders, notify_file, root=root
                    )
            self.assertEqual((pushed, skipped), (1, 0))
            self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
