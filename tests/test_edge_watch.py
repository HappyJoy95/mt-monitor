"""Tests for the debugging-browser watchdog (:mod:`src.mt_monitor.edge_watch`).

Every collaborator is a fake here: no real CDP connection, no process kill, no
Edge launch, no network.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.mt_monitor import edge_watch as ew
from src.mt_monitor.cli import main

FIXED_NOW = __import__("datetime").datetime(2026, 9, 17, 11, 20, 0)


class FakeEnv:
    """Records the watchdog's side effects instead of performing them."""

    def __init__(self, probe_results, pids=(111, 222), ready_after_restart=True):
        self.probe_results = list(probe_results)
        self.pids = list(pids)
        self.ready_after_restart = ready_after_restart
        self.killed = []
        self.launched = []
        self.waited = 0
        self.alerts = []
        self.probes = 0

    def probe(self, cdp_url, timeout=None):
        self.probes += 1
        if self.probe_results:
            return self.probe_results.pop(0)
        return False, "ConnectionRefusedError: 兜底"

    def pids_fn(self, profile_dir):
        return list(self.pids)

    def kill(self, pids):
        self.killed.append(list(pids))
        return list(pids)

    def launch(self, edge_exe, profile_dir, port, url):
        self.launched.append(
            {"edge_exe": edge_exe, "profile_dir": profile_dir, "port": port, "url": url}
        )
        return 4242

    def wait(self, cdp_url, timeout):
        self.waited += 1
        return self.ready_after_restart, "Edg/146 test"

    def alert(self, message):
        self.alerts.append(message)

    def run(self, tmp, threshold=3, edge_exe=r"C:\Edge\msedge.exe", **kwargs):
        state_path = Path(tmp) / "data" / "edge_watch_state.json"
        result = ew.run_check(
            cdp_url="http://127.0.0.1:9222",
            profile_dir=r"C:\tmp\mt-monitor-edge",
            edge_exe=edge_exe,
            state_path=state_path,
            threshold=threshold,
            alert_fn=self.alert,
            probe_fn=self.probe,
            pids_fn=self.pids_fn,
            kill_fn=self.kill,
            launch_fn=self.launch,
            wait_fn=self.wait,
            now_fn=lambda: FIXED_NOW,
            **kwargs,
        )
        return result, ew.load_state(state_path)


def _fake_edge(tmp) -> str:
    """Create a stand-in executable so the ``Path.exists()`` guard passes."""
    exe = Path(tmp) / "msedge.exe"
    exe.write_text("", encoding="utf-8")
    return str(exe)


class HealthyPathTest(unittest.TestCase):
    def test_healthy_probe_resets_counter_and_touches_nothing(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(True, "Edg/146.0.3856.78")])
            # Pre-seed a stale failure count to prove a success clears it.
            state_path = Path(directory) / "data" / "edge_watch_state.json"
            ew.save_state(state_path, ew.WatchdogState(consecutive_failures=2))

            result, state = env.run(directory)

        self.assertEqual(result["action"], "healthy")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.restarts, 0)
        self.assertEqual(state.last_detail, "Edg/146.0.3856.78")
        self.assertEqual(env.killed, [])
        self.assertEqual(env.launched, [])
        self.assertEqual(env.alerts, [])


class ThresholdTest(unittest.TestCase):
    def test_below_threshold_only_counts(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "TimeoutError: 超时")])

            result, state = env.run(directory, threshold=3)

        self.assertEqual(result["action"], "counted")
        self.assertEqual(result["consecutive_failures"], 1)
        self.assertEqual(state.consecutive_failures, 1)
        self.assertEqual(env.killed, [])
        self.assertEqual(env.alerts, [])

    def test_counter_accumulates_across_runs(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv(
                [
                    (False, "TimeoutError: 超时"),  # run 1
                    (False, "TimeoutError: 超时"),  # run 2
                    (False, "TimeoutError: 超时"),  # run 3 -> threshold
                ]
            )
            results = [env.run(directory, threshold=3)[0] for _ in range(3)]

        self.assertEqual(
            [r["action"] for r in results], ["counted", "counted", "restarted"]
        )

    def test_threshold_reached_restarts_and_alerts_once(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "TimeoutError: CDP 无响应")])
            exe = _fake_edge(directory)

            result, state = env.run(directory, threshold=1, edge_exe=exe)

        self.assertEqual(result["action"], "restarted")
        self.assertTrue(result["cdp_ready"])
        self.assertEqual(env.killed, [[111, 222]])
        self.assertEqual(len(env.launched), 1)
        self.assertEqual(env.launched[0]["profile_dir"], r"C:\tmp\mt-monitor-edge")
        self.assertEqual(env.launched[0]["port"], "9222")
        self.assertIn("shangoue.meituan.com", env.launched[0]["url"])
        self.assertEqual(env.waited, 1)
        self.assertEqual(state.restarts, 1)
        self.assertEqual(state.consecutive_failures, 0)  # spaced-out retries
        self.assertEqual(len(env.alerts), 1)
        alert = env.alerts[0]
        self.assertIn("已自动重启", alert)
        self.assertIn("CDP 探测连续 1 次失败", alert)
        self.assertIn("已就绪", alert)

    def test_restart_without_cdp_coming_back_is_reported(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "TimeoutError: CDP 无响应")], ready_after_restart=False)
            exe = _fake_edge(directory)

            result, state = env.run(directory, threshold=1, edge_exe=exe)

        self.assertEqual(result["action"], "restarted")
        self.assertFalse(result["cdp_ready"])
        self.assertIn("仍未就绪", env.alerts[0])
        self.assertEqual(state.restarts, 1)

    def test_no_edge_process_found_still_launches_one(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "ConnectionRefusedError: 端口未监听")], pids=[])
            exe = _fake_edge(directory)

            result, _ = env.run(directory, threshold=1, edge_exe=exe)

        self.assertEqual(result["killed"], [])
        self.assertEqual(len(env.launched), 1)
        self.assertEqual(result["action"], "restarted")

    def test_missing_edge_executable_is_reported_not_raised(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "TimeoutError: 超时")])

            result, state = env.run(directory, threshold=1, edge_exe="")

        self.assertEqual(result["action"], "restarted")
        self.assertFalse(result["cdp_ready"])
        self.assertIn("未找到 Edge 可执行文件", result["detail_after"])
        self.assertEqual(env.launched, [])
        self.assertEqual(len(env.alerts), 1)

    def test_alert_failure_never_breaks_the_check(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "TimeoutError: 超时")])
            exe = _fake_edge(directory)
            state_path = Path(directory) / "data" / "edge_watch_state.json"

            def boom(_message):
                raise RuntimeError("webhook 挂了")

            result = ew.run_check(
                edge_exe=exe,
                state_path=state_path,
                threshold=1,
                alert_fn=boom,
                probe_fn=env.probe,
                pids_fn=env.pids_fn,
                kill_fn=env.kill,
                launch_fn=env.launch,
                wait_fn=env.wait,
                now_fn=lambda: FIXED_NOW,
            )

        self.assertEqual(result["action"], "restarted")
        self.assertTrue(result["cdp_ready"])

    def test_launch_failure_is_reported_not_raised(self):
        with TemporaryDirectory() as directory:
            env = FakeEnv([(False, "TimeoutError: 超时")])
            exe = _fake_edge(directory)
            env.launch = lambda *a, **k: (_ for _ in ()).throw(OSError("启动失败"))

            result, _ = env.run(directory, threshold=1, edge_exe=exe)

        self.assertEqual(result["action"], "restarted")
        self.assertFalse(result["cdp_ready"])
        self.assertIn("启动失败", result["detail_after"])


class StateTest(unittest.TestCase):
    def test_missing_state_file_yields_defaults(self):
        with TemporaryDirectory() as directory:
            state = ew.load_state(Path(directory) / "nope.json")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.restarts, 0)

    def test_corrupt_state_file_yields_defaults(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{ not json", encoding="utf-8")
            state = ew.load_state(path)
        self.assertEqual(state.consecutive_failures, 0)

    def test_state_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            ew.save_state(path, ew.WatchdogState(consecutive_failures=2, restarts=1))
            state = ew.load_state(path)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(state.consecutive_failures, 2)
        self.assertEqual(state.restarts, 1)
        self.assertEqual(raw["consecutive_failures"], 2)


class HelperTest(unittest.TestCase):
    def test_cdp_port_extraction(self):
        self.assertEqual(ew.cdp_port("http://127.0.0.1:9222"), "9222")
        self.assertEqual(ew.cdp_port("http://127.0.0.1:9223/"), "9223")
        self.assertEqual(ew.cdp_port("http://127.0.0.1"), "9222")

    def test_probe_of_a_closed_port_fails_fast(self):
        ok, detail = ew.probe_cdp("http://127.0.0.1:45999", timeout=2.0)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_probe_bypasses_system_proxy(self):
        # A proxy must never be used for 127.0.0.1; the opener is built by hand.
        with mock.patch.object(ew.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = OSError("boom")
            ew.probe_cdp("http://127.0.0.1:9222", timeout=1.0)
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], ew.urllib.request.ProxyHandler)


class CliTest(unittest.TestCase):
    def test_healthy_check_exits_zero(self):
        with TemporaryDirectory() as directory:
            with mock.patch.object(
                ew,
                "run_check",
                return_value={"action": "healthy", "detail": "Edg/146"},
            ) as run:
                code = main(
                    [
                        "edge-watch",
                        "--no-alert",
                        "--root",
                        str(Path(directory)),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["threshold"], 3)
        self.assertEqual(run.call_args.kwargs["profile_dir"], r"C:\tmp\mt-monitor-edge")

    def test_failed_restart_exits_one(self):
        with TemporaryDirectory() as directory:
            with mock.patch.object(
                ew,
                "run_check",
                return_value={"action": "restarted", "cdp_ready": False},
            ):
                code = main(
                    ["edge-watch", "--no-alert", "--threshold", "1", "--root", str(Path(directory))]
                )
        self.assertEqual(code, 1)

    def test_counted_failure_still_exits_zero(self):
        with TemporaryDirectory() as directory:
            with mock.patch.object(
                ew,
                "run_check",
                return_value={"action": "counted", "consecutive_failures": 1},
            ):
                code = main(["edge-watch", "--no-alert", "--root", str(Path(directory))])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
