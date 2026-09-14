"""Tests for the unattended watch loop and its CLI wiring.

No browser, network or real clock is involved: ``run_forever`` takes its pull /
notify / alert / sleep collaborators as arguments, and the CLI tests patch the
bridge and the webhook alerter so ``watch`` can be exercised end to end.
"""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.mt_monitor import watch
from src.mt_monitor.cli import main

NO_SLEEP = lambda seconds: None  # noqa: E731 - tiny test double


class RunForeverTests(unittest.TestCase):
    def test_successful_cycle_runs_the_pull_and_the_push(self):
        summaries = []

        state = watch.run_forever(
            lambda: (Path("raw/a.json"), Path("data/a.json")),
            interval=1,
            notify_fn=summaries.append,
            sleep_fn=NO_SLEEP,
            once=True,
        )

        self.assertEqual(state.cycles, 1)
        self.assertEqual(summaries, [Path("data/a.json")])
        self.assertEqual(state.total_failures, 0)
        self.assertIsNone(state.last_error)

    def test_a_success_resets_the_consecutive_failure_counter(self):
        # fail, then succeed (counter reset), then fail again: max_failures=2
        # must never trip. The sentinel ends the test run.
        outcomes = iter([
            RuntimeError("第一次失败"),
            "ok",
            RuntimeError("成功之后再次失败"),
            StopIteration("测试结束"),
        ])
        cycles = []

        def pull_fn():
            cycles.append(1)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return Path("raw/a.json"), Path("data/a.json")

        with self.assertRaises(StopIteration):
            watch.run_forever(
                pull_fn,
                interval=1,
                max_failures=2,
                alert_after=0,
                sleep_fn=NO_SLEEP,
            )

        self.assertEqual(len(cycles), 4)

    def test_exhausted_iterator_is_not_mistaken_for_a_pull_failure(self):
        # A bare `except Exception` would turn an exhausted test/producer
        # iterator into an endless loop of fake failures.
        with self.assertRaises(StopIteration):
            watch.run_forever(
                lambda: (_ for _ in ()).throw(StopIteration()),
                interval=1,
                sleep_fn=NO_SLEEP,
            )

    def test_consecutive_failures_stop_the_loop_at_max_failures(self):
        calls = []

        def pull_fn():
            calls.append(1)
            raise RuntimeError("抓不到订单")

        alerts = []
        state = watch.run_forever(
            pull_fn,
            interval=1,
            max_failures=2,
            alert_after=1,
            alert_fn=alerts.append,
            sleep_fn=NO_SLEEP,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(state.consecutive_failures, 2)
        self.assertIn("达到上限", state.stop_reason)
        # Alert once at the threshold, then the distinct exit alert — not
        # two overlapping alerts on the final attempt.
        self.assertEqual(len(alerts), 2)
        self.assertIn("⚠️", alerts[0])
        self.assertIn("🛑", alerts[1])

    def test_alert_failure_never_kills_the_loop(self):
        def pull_fn():
            raise RuntimeError("抓不到订单")

        def bad_alert(message):
            raise ConnectionError("webhook 挂了")

        state = watch.run_forever(
            pull_fn,
            interval=1,
            max_failures=2,
            alert_after=1,
            alert_fn=bad_alert,
            sleep_fn=NO_SLEEP,
        )

        self.assertEqual(state.alerts_sent, 0)
        self.assertIn("达到上限", state.stop_reason)

    def test_notify_failure_is_logged_but_not_counted_as_pull_failure(self):
        def bad_notify(path):
            raise RuntimeError("push 失败")

        state = watch.run_forever(
            lambda: (Path("raw/a.json"), Path("data/a.json")),
            interval=1,
            notify_fn=bad_notify,
            sleep_fn=NO_SLEEP,
            once=True,
        )

        self.assertEqual(state.total_failures, 0)
        self.assertEqual(state.cycles, 1)


class WatchCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        summary = self.root / "data" / "latest-new-orders.json"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(json.dumps([]), encoding="utf-8")
        self.summary = str(summary)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv, outcomes):
        """Run `watch` with a bridge double popping one outcome per pull."""
        queue = list(outcomes)
        bridge = mock.Mock()

        def pull(root, **kwargs):
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return Path("raw/x.json"), Path(outcome)

        bridge.pull_order_list.side_effect = pull
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "src.mt_monitor.cli._load_pull_dependencies", return_value=bridge
        ), mock.patch.object(
            watch, "make_webhook_alerter", return_value=lambda message: None
        ), mock.patch.object(
            watch, "run_forever", wraps=watch.run_forever
        ) as spy:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv + ["--root", str(self.root)])
        return code, out.getvalue(), err.getvalue(), spy, bridge

    def test_once_mode_succeeds_and_reports(self):
        code, output, _err, _spy, _bridge = self._run(
            ["watch", "--once", "--interval", "1", "--alert-after", "0"],
            [self.summary],
        )

        self.assertEqual(code, 0)
        self.assertIn("watch 启动", output)
        self.assertIn("第 1 次拉取成功", output)
        self.assertIn("watch 结束", output)

    def test_failures_reaching_the_limit_exit_nonzero(self):
        code, output, _err, spy, bridge = self._run(
            ["watch", "--interval", "1", "--max-failures", "2", "--alert-after", "0"],
            [RuntimeError("页面卡死"), RuntimeError("页面卡死")],
        )

        self.assertEqual(code, 1)
        self.assertEqual(bridge.pull_order_list.call_count, 2)
        self.assertIn("连续 2 次", output)
        self.assertEqual(spy.call_args.kwargs["max_failures"], 2)

    def test_retries_flag_reaches_the_bridge_attempt_count(self):
        _code, _out, _err, _spy, bridge = self._run(
            ["watch", "--once", "--interval", "1", "--retries", "4"],
            [self.summary],
        )

        self.assertEqual(
            bridge.pull_order_list.call_args.kwargs["max_attempts"], 5
        )

    def test_stuck_page_logs_the_recovery_message(self):
        code, output, _err, _spy, _bridge = self._run(
            ["watch", "--once", "--interval", "1", "--alert-after", "0"],
            [RuntimeError("刷新重试 3 次后仍未捕获到订单列表")],
        )

        self.assertEqual(code, 0)  # --once exits 0 even when the pull failed
        self.assertIn("拉取失败", output)
        self.assertIn("连续 1 次", output)


if __name__ == "__main__":
    unittest.main()
