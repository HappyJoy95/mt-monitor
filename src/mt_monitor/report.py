"""Audit the per-minute pull schedule: match missing minutes to their run log.

Two artefacts make the schedule traceable:

* ``raw/YYYY-MM-DDTHH-MM-SS+0800-order-list.json`` — one file per *successful*
  capture, so a missing minute means "that minute produced no capture".
* ``logs/pull-YYYY-MM-DD.log`` (written by ``run_pull.cmd``) — a ``pull start``
  line and a ``pull end (exit=N)`` line per scheduled run, plus whatever the
  process printed in between.

Comparing the two answers "which minutes did not execute, and why": no start
line at all means the task never fired; a start line with a non-zero exit code
means the run failed (the printed reason is quoted); a start line without an end
line means the run was still in flight when the report was produced.

Timestamps in both artefacts are local time; the raw filename carries the local
``+0800`` offset, which is ignored here (same machine, same timezone).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

RAW_NAME_RE = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}-\d{2}-\d{2})\+\d{4}-order-list\.json$"
)
# `%time:~0,8%` in cmd keeps a leading space for single-digit hours, hence \s+.
LOG_START_RE = re.compile(
    r"^\[(?P<day>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{1,2}:\d{2}:\d{2})\]\s*---\s*pull start\s*---\s*$"
)
LOG_END_RE = re.compile(
    r"^\[(?P<day>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{1,2}:\d{2}:\d{2})\]\s*---\s*pull end"
    r"\s*\(exit=(?P<code>-?\d+)\)\s*---\s*$"
)

# The scheduled window configured on this machine (run_pull.cmd / MT Monitor).
WINDOW_START = time(8, 30)
WINDOW_END = time(22, 0)

NO_RUN = "未运行"
FAILED = "运行失败"
RUNNING = "运行中"
NO_OUTPUT = "运行成功但无输出"
BLOCKED = "上一轮仍在运行"
NO_LOG = "无日志记录"

_DIAGNOSTIC_MARKERS = ("失败", "⚠️", "Error", "error", "Timeout", "Traceback")


@dataclass
class RunRecord:
    """One scheduled run reconstructed from the run log."""

    start: datetime
    end: datetime | None = None
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def span_minutes(self) -> int:
        """Minutes this run occupied (at least one)."""
        if self.end is None:
            return 1
        return max(1, int((self.end - self.start).total_seconds() // 60) + 1)

    def diagnostic(self) -> str:
        """First diagnostic line printed by the run, or "" when clean."""
        for line in self.lines:
            if any(marker in line for marker in _DIAGNOSTIC_MARKERS):
                return line.strip()
        return ""


@dataclass
class Gap:
    """A minute inside the monitoring window that produced no capture."""

    minute: datetime
    kind: str
    detail: str = ""

    def render(self) -> str:
        stamp = self.minute.strftime("%H:%M")
        return f"  {stamp}  {self.kind}" + (f"  {self.detail}" if self.detail else "")


@dataclass
class AuditReport:
    """Result of :func:`audit`."""

    day: date
    window_start: datetime
    window_end: datetime
    captured: list[datetime] = field(default_factory=list)
    duplicates: list[datetime] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    log_path: Path | None = None

    @property
    def failed_runs(self) -> list[RunRecord]:
        return [r for r in self.runs if r.exit_code not in (None, 0)]

    @property
    def ok(self) -> bool:
        return not self.gaps


def parse_raw_minutes(root: Path, day: date) -> tuple[list[datetime], list[datetime]]:
    """Return ``(captured_minutes, duplicated_minutes)`` for ``day`` from ``raw/``."""
    seen: dict[datetime, int] = {}
    for path in (Path(root) / "raw").glob("*-order-list.json"):
        match = RAW_NAME_RE.match(path.name)
        if match is None:
            continue
        if match.group("day") != day.isoformat():
            continue
        stamp = datetime.fromisoformat(
            f"{match.group('day')}T{match.group('time').replace('-', ':')}"
        )
        minute = stamp.replace(second=0, microsecond=0)
        seen[minute] = seen.get(minute, 0) + 1
    captured = sorted(seen)
    duplicates = sorted(m for m, count in seen.items() if count > 1)
    return captured, duplicates


def parse_run_log(path: Path | str) -> list[RunRecord]:
    """Parse ``logs/pull-YYYY-MM-DD.log`` into :class:`RunRecord` objects."""
    p = Path(path)
    if not p.exists():
        return []
    records: list[RunRecord] = []
    current: RunRecord | None = None
    for raw_line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.rstrip()
        start_match = LOG_START_RE.match(line)
        if start_match:
            current = RunRecord(
                start=datetime.strptime(
                    f"{start_match.group('day')} {start_match.group('time')}",
                    "%Y-%m-%d %H:%M:%S",
                )
            )
            records.append(current)
            continue
        end_match = LOG_END_RE.match(line)
        if end_match and current is not None:
            current.end = datetime.strptime(
                f"{end_match.group('day')} {end_match.group('time')}",
                "%Y-%m-%d %H:%M:%S",
            )
            current.exit_code = int(end_match.group("code"))
            continue
        if current is not None and line.strip():
            current.lines.append(line)
    return records


def _classify(
    minute: datetime,
    runs: list[RunRecord],
    captured_minutes: set[datetime],
    log_since: datetime | None,
) -> Gap | None:
    """Explain why ``minute`` has no capture, or return ``None`` when it does."""
    if minute in captured_minutes:
        return None
    minute_end = minute + timedelta(minutes=1)
    starters = [r for r in runs if minute <= r.start < minute_end]
    if starters:
        run = starters[-1]
        if run.exit_code is None:
            return Gap(minute, RUNNING, "该轮尚未结束（可能仍在抓取/推送）")
        if run.exit_code == 0:
            return Gap(minute, NO_OUTPUT, "退出码为 0 但没有落盘，需检查磁盘/权限")
        return Gap(minute, FAILED, f"exit={run.exit_code} {run.diagnostic()}".strip())
    spanning = [r for r in runs if r.start < minute and (r.end is None or r.end > minute)]
    if spanning:
        run = spanning[0]
        return Gap(
            minute,
            BLOCKED,
            f"上一轮 {run.start.strftime('%H:%M:%S')} 起仍在运行"
            f"（{run.span_minutes} 分钟）",
        )
    if log_since is None or minute < log_since:
        # The run log did not exist yet (or has no entry that early), so the
        # cause cannot be determined from it — say so instead of guessing.
        detail = (
            f"日志最早记录 {log_since.strftime('%H:%M:%S')}，该分钟无法判定"
            if log_since is not None
            else "该日无运行日志，无法判定（日志从部署 run_pull.cmd 起才有）"
        )
        return Gap(minute, NO_LOG, detail)
    return Gap(minute, NO_RUN, "任务未触发（计划任务/机器状态）")


def audit(
    root: Path | str,
    day: date | None = None,
    *,
    now: datetime | None = None,
    window_start: time = WINDOW_START,
    window_end: time = WINDOW_END,
) -> AuditReport:
    """Compare the configured window against captures and the run log."""
    root = Path(root)
    now = now or datetime.now()
    day = day or now.date()

    start_dt = datetime.combine(day, window_start)
    end_dt = datetime.combine(day, window_end)
    if day == now.date():
        end_dt = min(end_dt, now)
    elif end_dt > now:
        end_dt = now

    captured, duplicates = parse_raw_minutes(root, day)
    log_path = root / "logs" / f"pull-{day.isoformat()}.log"
    runs = [r for r in parse_run_log(log_path) if r.start.date() == day]

    captured_set = set(captured)
    log_since = min((r.start for r in runs), default=None)
    gaps: list[Gap] = []
    minute = start_dt
    while minute <= end_dt:
        gap = _classify(minute, runs, captured_set, log_since)
        if gap is not None:
            gaps.append(gap)
        minute += timedelta(minutes=1)

    return AuditReport(
        day=day,
        window_start=start_dt,
        window_end=end_dt,
        captured=captured,
        duplicates=duplicates,
        gaps=gaps,
        runs=runs,
        log_path=log_path,
    )


def format_report(report: AuditReport) -> str:
    """Render an :class:`AuditReport` as plain text for the console."""
    total_minutes = int((report.window_end - report.window_start).total_seconds() // 60) + 1
    lines = [
        f"日期：{report.day.isoformat()}   "
        f"窗口：{report.window_start.strftime('%H:%M')} ~ "
        f"{report.window_end.strftime('%H:%M')}（{total_minutes} 分钟）",
        f"成功抓取：{len(report.captured)} 分钟"
        + (f"（其中 {len(report.duplicates)} 分钟有重复抓取）" if report.duplicates else ""),
        f"计划运行：{len(report.runs)} 轮，失败 {len(report.failed_runs)} 轮",
    ]
    if report.log_path is not None:
        lines.append(
            f"运行日志：{report.log_path}"
            + ("" if report.log_path.exists() else "（不存在）")
        )
    if not report.gaps:
        lines.append("缺失：无 ✅ 每分钟都执行了")
        return "\n".join(lines)

    lines.append(f"缺失：{len(report.gaps)} 分钟")
    lines.extend(gap.render() for gap in report.gaps)
    return "\n".join(lines)
