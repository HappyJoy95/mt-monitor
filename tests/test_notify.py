import json
import os
import shutil
import unittest
from datetime import datetime
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


class StoreWebhookTest(unittest.TestCase):
    """Tests for per-store webhook routing."""

    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_notify_store"
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / "config").mkdir(exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_push_during_business_hours(self):
        """Order should be pushed to store webhook during business hours."""
        store_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=STORE123"
        self.tmp.joinpath("config", "store_webhooks.json").write_text(
            json.dumps(
                [
                    {
                        "门店名": "测试门店",
                        "营业开始时间": "00:00:00",
                        "营业结束时间": "23:59:59",
                        "webhook": store_url,
                    }
                ]
            ),
            encoding="utf-8",
        )
        orders = [{"order_id": "O1", "status": "待接单", "store": "测试门店"}]
        with mock.patch.object(
            WechatWebhookClient, "send_text", return_value=None
        ) as m:
            pushed, skipped = notify.process_notifications(
                orders, Path("/nonexistent/notify"), root=self.tmp
            )
        self.assertEqual(pushed, 1)
        self.assertEqual(m.call_count, 1)

    def test_store_push_skipped_outside_business_hours(self):
        """Order should NOT be pushed to store webhook outside business hours."""
        store_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=STORE123"
        self.tmp.joinpath("config", "store_webhooks.json").write_text(
            json.dumps(
                [
                    {
                        "门店名": "测试门店",
                        "营业开始时间": "01:00:00",
                        "营业结束时间": "02:00:00",
                        "webhook": store_url,
                    }
                ]
            ),
            encoding="utf-8",
        )
        orders = [{"order_id": "O1", "status": "待接单", "store": "测试门店"}]
        with mock.patch.object(
            WechatWebhookClient, "send_text", return_value=None
        ) as m:
            pushed, skipped = notify.process_notifications(
                orders, Path("/nonexistent/notify"), root=self.tmp
            )
        self.assertEqual(pushed, 0)
        self.assertEqual(m.call_count, 0)

    def test_unmapped_store_skips_store_push(self):
        """Order for a store not in the mapping should only try default webhook."""
        orders = [{"order_id": "O1", "status": "待接单", "store": "未映射门店"}]
        with mock.patch.object(
            WechatWebhookClient, "send_text", return_value=None
        ) as m:
            pushed, skipped = notify.process_notifications(
                orders, Path("/nonexistent/notify"), root=self.tmp
            )
        self.assertEqual(pushed, 0)
        self.assertEqual(m.call_count, 0)

    def test_default_and_store_both_push(self):
        """With both default and store webhooks, both should be called."""
        default_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=DEFAULT"
        store_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=STORE123"
        self.tmp.joinpath("config", "notify").write_text(default_url, encoding="utf-8")
        self.tmp.joinpath("config", "store_webhooks.json").write_text(
            json.dumps(
                [
                    {
                        "门店名": "测试门店",
                        "营业开始时间": "00:00:00",
                        "营业结束时间": "23:59:59",
                        "webhook": store_url,
                    }
                ]
            ),
            encoding="utf-8",
        )
        orders = [{"order_id": "O1", "status": "待接单", "store": "测试门店"}]
        with mock.patch.object(
            WechatWebhookClient, "send_text", return_value=None
        ) as m:
            pushed, skipped = notify.process_notifications(
                orders, self.tmp / "config" / "notify", root=self.tmp
            )
        self.assertEqual(pushed, 1)
        self.assertEqual(m.call_count, 2)


class MidnightCrossingTest(unittest.TestCase):
    """Tests for business hours that cross midnight."""

    def test_crossing_midnight_in_range(self):
        with mock.patch("src.mt_monitor.notify.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 23, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertTrue(notify._is_within_business_hours("22:00:00", "06:00:00"))

    def test_crossing_midnight_out_range(self):
        with mock.patch("src.mt_monitor.notify.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertFalse(notify._is_within_business_hours("22:00:00", "06:00:00"))


if __name__ == "__main__":
    unittest.main()
