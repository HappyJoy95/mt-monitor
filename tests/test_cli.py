import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from src.mt_monitor.cli import cmd_pull_logged, main, run_logged
from src.mt_monitor.report import parse_run_log


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


class RunLoggedTests(unittest.TestCase):
    """The scheduled runner logs per run and must survive overlapping runs.

    Regression guard: the log used to be written by the .cmd wrapper with ">>",
    which a concurrent run could not open — the tick then exited 0 having done
    nothing (2026-09-18 19:55) and the audit could not explain it.
    """

    def test_writes_start_and_end_block_with_exit_code(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            code = run_logged(root, lambda: (print("原始数据：x"), 0)[1])

            self.assertEqual(code, 0)
            body = (root / "logs" / f"pull-{_today()}.log").read_text(encoding="utf-8")
        self.assertIn("--- pull start ---", body)
        self.assertIn("--- pull end (exit=0) ---", body)
        self.assertIn("原始数据：x", body)

    def test_returns_the_wrapped_exit_code_verbatim(self):
        with TemporaryDirectory() as directory:
            code = run_logged(Path(directory), lambda: 1)
        self.assertEqual(code, 1)

    def test_nonzero_run_still_gets_a_complete_block(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def failing():
                print("拉取失败：无法连接浏览器 CDP")
                return 1

            run_logged(root, failing)
            body = (root / "logs" / f"pull-{_today()}.log").read_text(encoding="utf-8")

        self.assertIn("拉取失败：无法连接浏览器 CDP", body)
        self.assertIn("--- pull end (exit=1) ---", body)

    def test_appends_instead_of_overwriting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_logged(root, lambda: 0)
            run_logged(root, lambda: 0)
            body = (root / "logs" / f"pull-{_today()}.log").read_text(encoding="utf-8")

        self.assertEqual(body.count("--- pull start ---"), 2)
        self.assertEqual(body.count("--- pull end"), 2)

    def test_block_is_parseable_by_the_audit_tooling(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_logged(root, lambda: 0)
            runs = parse_run_log(root / "logs" / f"pull-{_today()}.log")

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].exit_code, 0)
        self.assertIsNotNone(runs[0].end)

    def test_empty_output_produces_no_blank_garbage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_logged(root, lambda: 0)
            lines = (
                root / "logs" / f"pull-{_today()}.log"
            ).read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("--- pull start ---"))
        self.assertTrue(lines[1].endswith("--- pull end (exit=0) ---"))

    def test_stdout_is_echoed_to_the_console(self):
        with TemporaryDirectory() as directory:
            captured = io.StringIO()
            with redirect_stdout_for_test(captured):
                run_logged(Path(directory), lambda: (print("订单摘要：3 笔"), 0)[1])
        self.assertIn("订单摘要：3 笔", captured.getvalue())

    def test_cmd_pull_logged_passes_retries_through(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("src.mt_monitor.cli.cmd_pull", return_value=0) as pull:
                code = cmd_pull_logged(root, "http://127.0.0.1:9222", 30, retries=1)
                pull.assert_called_once()
                self.assertEqual(pull.call_args.kwargs["retries"], 1)
        self.assertEqual(code, 0)


def _today() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def redirect_stdout_for_test(target):
    from contextlib import redirect_stdout

    return redirect_stdout(target)
