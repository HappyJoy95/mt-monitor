"""Tests for the pull-schedule audit (:mod:`src.mt_monitor.report`)."""
import unittest
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

from src.mt_monitor.report import (
    BLOCKED,
    FAILED,
    NO_LOG,
    NO_OUTPUT,
    NO_RUN,
    RUNNING,
    audit,
    format_report,
    parse_raw_minutes,
    parse_run_log,
)

DAY = date(2026, 9, 14)
# Window floor = start of 10:02, so a run of "10:00 / 10:01 / 10:02" is the
# whole window and nothing after 10:02 is reported as a future gap.
NOW = datetime(2026, 9, 14, 10, 2, 59)
WINDOW_START = time(10, 0)


def _raw(root: Path, minute_second: str) -> None:
    """Create a raw capture file named the way storage.save_raw() names them."""
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"2026-09-14T{minute_second}+0800-order-list.json").write_text(
        "{}", encoding="utf-8"
    )


def _log(root: Path, body: str) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "pull-2026-09-14.log").write_text(body, encoding="utf-8")


def _run_block(start: str, end: str, exit_code: int, reason: str = "") -> str:
    lines = [f"[2026-09-14 {start}] --- pull start ---"]
    if reason:
        lines.append(reason)
    lines.append(f"[2026-09-14 {end}] --- pull end (exit={exit_code}) ---")
    return "\n".join(lines) + "\n"


def _minutes(gaps):
    return [g.minute.strftime("%H:%M") for g in gaps]


def _kinds(gaps):
    return [g.kind for g in gaps]


class ParseRawMinutesTest(unittest.TestCase):
    def test_splits_minutes_and_detects_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "09-40-11")
            _raw(root, "09-40-59")  # same minute captured twice
            _raw(root, "09-41-12")
            (root / "raw" / "2026-09-13T09-41-00+0800-order-list.json").write_text(
                "{}", encoding="utf-8"
            )

            captured, duplicates = parse_raw_minutes(root, DAY)

        self.assertEqual([c.strftime("%H:%M") for c in captured], ["09:40", "09:41"])
        self.assertEqual([d.strftime("%H:%M") for d in duplicates], ["09:40"])


class ParseRunLogTest(unittest.TestCase):
    def test_reads_start_end_exit_and_keeps_printed_lines(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _log(
                root,
                _run_block("09:40:01", "09:40:08", 0)
                + _run_block(
                    "09:41:01",
                    "09:41:31",
                    1,
                    "拉取失败：无法连接浏览器 CDP（http://127.0.0.1:9222）",
                ),
            )
            runs = parse_run_log(root / "logs" / "pull-2026-09-14.log")

        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].start.strftime("%H:%M:%S"), "09:40:01")
        self.assertEqual(runs[0].end.strftime("%H:%M:%S"), "09:40:08")
        self.assertEqual(runs[0].exit_code, 0)
        self.assertEqual(runs[0].diagnostic(), "")
        self.assertEqual(runs[1].exit_code, 1)
        self.assertIn("无法连接浏览器 CDP", runs[1].diagnostic())
        self.assertEqual(runs[1].span_minutes, 1)

    def test_single_digit_hour_with_leading_space_is_parsed(self):
        # cmd's %time:~0,8% keeps a leading space for hours below 10.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _log(
                root,
                "[2026-09-14  9:05:03] --- pull start ---\n"
                "[2026-09-14  9:05:11] --- pull end (exit=0) ---\n",
            )
            runs = parse_run_log(root / "logs" / "pull-2026-09-14.log")

        self.assertEqual(runs[0].start.strftime("%H:%M:%S"), "09:05:03")
        self.assertEqual(runs[0].exit_code, 0)

    def test_missing_log_file_yields_nothing(self):
        with TemporaryDirectory() as directory:
            runs = parse_run_log(Path(directory) / "logs" / "nope.log")
        self.assertEqual(runs, [])


class AuditTest(unittest.TestCase):
    def test_continuous_minutes_report_no_gap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for second in (0, 1, 2):
                _raw(root, f"10-0{second}-05")
            _log(
                root,
                _run_block("10:00:01", "10:00:06", 0)
                + _run_block("10:01:01", "10:01:06", 0)
                + _run_block("10:02:01", "10:02:06", 0),
            )

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertTrue(report.ok)
        self.assertEqual(report.gaps, [])
        self.assertIn("缺失：无", format_report(report))

    def test_missing_minute_without_log_entry_is_flagged_as_not_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")
            _raw(root, "10-02-05")  # 10:01 produced no capture
            _log(
                root,
                _run_block("10:00:01", "10:00:06", 0)
                + _run_block("10:02:01", "10:02:06", 0),
            )

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_minutes(report.gaps), ["10:01"])
        self.assertEqual(_kinds(report.gaps), [NO_RUN])
        self.assertIn("10:01", format_report(report))

    def test_failed_run_reason_is_quoted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")
            _raw(root, "10-02-05")
            _log(
                root,
                _run_block("10:00:01", "10:00:06", 0)
                + _run_block(
                    "10:01:01", "10:01:31", 1, "拉取失败：超时未捕获到订单列表接口响应。"
                )
                + _run_block("10:02:01", "10:02:06", 0),
            )

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_minutes(report.gaps), ["10:01"])
        self.assertEqual(_kinds(report.gaps), [FAILED])
        self.assertIn("exit=1", report.gaps[0].detail)
        self.assertIn("拉取失败", report.gaps[0].detail)
        self.assertEqual(len(report.failed_runs), 1)
        self.assertIn("运行失败", format_report(report))

    def test_minutes_before_the_log_existed_are_reported_as_unknown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-02-05")
            _log(root, _run_block("10:02:01", "10:02:06", 0))

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_minutes(report.gaps), ["10:00", "10:01"])
        self.assertEqual(_kinds(report.gaps), [NO_LOG, NO_LOG])
        self.assertIn("日志最早记录", report.gaps[0].detail)

    def test_day_without_any_log_is_reported_as_undeterminable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_kinds(report.gaps), [NO_LOG, NO_LOG])
        self.assertIn("无运行日志", report.gaps[0].detail)

    def test_still_running_minute_is_not_a_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")
            _log(root, "[2026-09-14 10:01:01] --- pull start ---\n")

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_kinds(report.gaps), [RUNNING, BLOCKED])
        self.assertEqual(report.failed_runs, [])
        self.assertIn("尚未结束", report.gaps[0].detail)

    def test_zero_exit_without_capture_is_flagged_as_no_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")
            _log(root, _run_block("10:01:01", "10:01:04", 0))

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_kinds(report.gaps), [NO_OUTPUT, NO_RUN])
        self.assertEqual(_minutes(report.gaps), ["10:01", "10:02"])

    def test_minutes_covered_by_a_long_run_are_reported_as_blocked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")
            # One run from 10:00:30 to 10:02:30 spans 10:01 and 10:02.
            _log(root, _run_block("10:00:30", "10:02:30", 0))

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(_minutes(report.gaps), ["10:01", "10:02"])
        self.assertEqual(_kinds(report.gaps), [BLOCKED, BLOCKED])
        self.assertIn("上一轮", report.gaps[0].detail)

    def test_duplicate_captures_are_surfaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "10-00-05")
            _raw(root, "10-00-40")  # same minute, two runs (Parallel policy)
            _raw(root, "10-01-05")
            _raw(root, "10-02-05")

            report = audit(root, DAY, now=NOW, window_start=WINDOW_START)

        self.assertEqual(
            [d.strftime("%H:%M") for d in report.duplicates], ["10:00"]
        )
        self.assertTrue(report.ok)
        self.assertIn("重复抓取", format_report(report))

    def test_window_end_is_clamped_to_now_and_future_minutes_are_ignored(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _raw(root, "09-59-05")
            _raw(root, "10-00-05")

            report = audit(root, DAY, now=NOW, window_start=time(9, 59))

        self.assertEqual(report.window_end, NOW)
        # 09:59 and 10:00 captured; 10:01/10:02 have no capture nor log.
        self.assertEqual(_minutes(report.gaps), ["10:01", "10:02"])


if __name__ == "__main__":
    unittest.main()
