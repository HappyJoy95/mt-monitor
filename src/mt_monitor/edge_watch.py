"""Watchdog for the dedicated debugging Edge used by ``pull``.

Why this exists
---------------
``bridge`` self-heals *pages* (stuck renderer, blank tab, detached frame), but it
cannot heal a *wedged browser process*: the port keeps listening while the CDP
handshake times out, so every scheduled ``pull`` fails at the very first step
and the self-healing ladder never runs. On 2026-09-16 that cost ~12 hours of
monitoring, so this watchdog probes CDP on a schedule and, after a few
consecutive failures, restarts only the Edge instance that uses the monitor's
own user-data-dir (the user's normal browsing window is never touched) and
alerts the main Enterprise WeChat group.

Design notes
------------
* One JSON state file (``data/edge_watch_state.json``) carries the consecutive
  failure counter between scheduled runs, so the threshold means "N checks in a
  row", not "N probes in one process".
* The counter resets after every restart attempt, which spaces retries out to
  the threshold interval instead of restarting the browser every single run.
* Every collaborator is injectable (``probe_fn`` / ``pids_fn`` / ``kill_fn`` /
  ``launch_fn`` / ``wait_fn`` / ``alert_fn``) so the decision logic is testable
  without a browser, a network or a real process tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_PROFILE_DIR = r"C:\tmp\mt-monitor-edge"
DEFAULT_THRESHOLD = 3
DEFAULT_PROBE_TIMEOUT = 5.0
DEFAULT_STARTUP_TIMEOUT = 60.0
DEFAULT_STARTUP_POLL = 2.0
DEFAULT_ORDER_URL = (
    "https://shangoue.meituan.com/#/page/orderbusiness#/order/unprocessed"
)
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
STATE_FILE = Path("data/edge_watch_state.json")


def log(message: str) -> None:
    """Timestamped, immediately flushed progress line."""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def cdp_port(cdp_url: str, default: str = "9222") -> str:
    """Extract the port from a CDP URL (``http://127.0.0.1:9223`` -> ``9223``).

    Falls back to ``default`` when the URL carries no port, so the relaunch
    always targets a concrete ``--remote-debugging-port``.
    """
    from urllib.parse import urlparse

    parsed = urlparse(cdp_url if "//" in cdp_url else f"//{cdp_url}", scheme="http")
    return str(parsed.port) if parsed.port else default


def find_edge_exe() -> str:
    """First existing Edge executable, or "" when none of the known paths exist."""
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def probe_cdp(cdp_url: str, timeout: float = DEFAULT_PROBE_TIMEOUT) -> tuple[bool, str]:
    """HTTP-probe ``/json/version``; returns ``(ok, detail)``.

    A wedged browser accepts the TCP connection but never answers, so the probe
    uses a short timeout and bypasses any system HTTP proxy (127.0.0.1 must not
    be tunnelled).
    """
    url = cdp_url.rstrip("/") + "/json/version"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
        info = json.loads(body)
        return True, str(info.get("Browser", "CDP OK"))
    except Exception as exc:  # noqa: BLE001 - any failure is "not healthy"
        return False, f"{type(exc).__name__}: {exc}"


def find_edge_pids(profile_dir: str) -> list[int]:
    """PIDs of ``msedge.exe`` processes started with ``--user-data-dir=<profile>``.

    Only the monitor's own profile is matched, so the user's normal Edge window
    is never a candidate. Returns ``[]`` off Windows or when nothing matches.
    """
    if os.name != "nt":
        return []
    safe = profile_dir.replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{safe}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def kill_pids(pids: list[int]) -> list[int]:
    """Force-kill the given process trees; returns the PIDs that were killed."""
    killed = []
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            killed.append(pid)
        except (OSError, subprocess.SubprocessError):
            continue
    return killed


def launch_edge(
    edge_exe: str,
    profile_dir: str,
    port: str = "9222",
    url: str = DEFAULT_ORDER_URL,
) -> int:
    """Start a detached debugging Edge on the monitor profile; returns its PID."""
    args = [
        edge_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
    ]
    if url:
        args.append(url)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    proc = subprocess.Popen(args, creationflags=flags, close_fds=True)
    return proc.pid


def wait_for_cdp(
    cdp_url: str,
    timeout: float = DEFAULT_STARTUP_TIMEOUT,
    poll: float = DEFAULT_STARTUP_POLL,
    probe_fn=probe_cdp,
) -> tuple[bool, str]:
    """Poll CDP until it answers or ``timeout`` elapses."""
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    ok, detail = probe_fn(cdp_url)
    while not ok and time.monotonic() < deadline:
        time.sleep(poll)
        ok, detail = probe_fn(cdp_url)
    return ok, detail


@dataclass
class WatchdogState:
    """Counters persisted between scheduled watchdog runs."""

    consecutive_failures: int = 0
    restarts: int = 0
    last_check: str | None = None
    last_detail: str = ""


def load_state(path: Path | str) -> WatchdogState:
    p = Path(path)
    if not p.exists():
        return WatchdogState()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return WatchdogState(
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            restarts=int(data.get("restarts", 0)),
            last_check=data.get("last_check"),
            last_detail=str(data.get("last_detail", "")),
        )
    except (OSError, ValueError, TypeError):
        return WatchdogState()


def save_state(path: Path | str, state: WatchdogState) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _alert(alert_fn, state: WatchdogState, message: str) -> None:
    if alert_fn is None:
        return
    try:
        alert_fn(message)
    except Exception as exc:  # noqa: BLE001 - an unreachable webhook must not fail the check
        log(f"告警发送失败（已忽略）：{type(exc).__name__}: {exc}")


def run_check(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    edge_exe: str = "",
    state_path: Path | str = STATE_FILE,
    threshold: int = DEFAULT_THRESHOLD,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    order_url: str = DEFAULT_ORDER_URL,
    alert_fn=None,
    probe_fn=None,
    pids_fn=None,
    kill_fn=None,
    launch_fn=None,
    wait_fn=None,
    now_fn=None,
) -> dict:
    """Probe CDP and restart the monitor's Edge once failures hit ``threshold``.

    Returns a summary dict with ``action`` in ``{"healthy", "counted",
    "restarted"}`` plus details; ``restarted`` also carries ``cdp_ready`` so a
    caller (or the exit code) can tell whether the browser actually came back.

    The collaborators default to the module-level implementations and are
    resolved at call time, so tests can monkey-patch them.
    """
    probe_fn = probe_fn or probe_cdp
    pids_fn = pids_fn or find_edge_pids
    kill_fn = kill_fn or kill_pids
    launch_fn = launch_fn or launch_edge
    wait_fn = wait_fn or wait_for_cdp
    now_fn = now_fn or datetime.now

    threshold = max(1, int(threshold))
    state = load_state(state_path)
    ok, detail = probe_fn(cdp_url, probe_timeout)
    state.last_check = now_fn().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    state.last_detail = detail

    if ok:
        if state.consecutive_failures:
            log(f"CDP 恢复正常（此前连续失败 {state.consecutive_failures} 次）：{detail}")
        state.consecutive_failures = 0
        save_state(state_path, state)
        log(f"CDP 正常：{detail}")
        return {"action": "healthy", "detail": detail, "restarts": state.restarts}

    state.consecutive_failures += 1
    log(
        f"CDP 探测失败（连续 {state.consecutive_failures}/{threshold} 次）：{detail}"
    )
    if state.consecutive_failures < threshold:
        save_state(state_path, state)
        return {
            "action": "counted",
            "detail": detail,
            "consecutive_failures": state.consecutive_failures,
            "threshold": threshold,
            "restarts": state.restarts,
        }

    failures = state.consecutive_failures
    pids = pids_fn(profile_dir)
    if pids:
        killed = kill_fn(pids)
        log(f"已结束调试 Edge 进程：{killed or pids}（profile={profile_dir}）")
    else:
        log(f"未找到调试 Edge 进程（profile={profile_dir}），直接拉起")
        killed = []

    ready = False
    launched = 0
    if edge_exe and Path(edge_exe).exists():
        try:
            launched = launch_fn(edge_exe, profile_dir, cdp_port(cdp_url), order_url)
            log(f"已拉起 Edge：pid={launched}")
            ready, ready_detail = wait_fn(cdp_url, startup_timeout)
        except Exception as exc:  # noqa: BLE001 - report, never crash the task
            ready_detail = f"{type(exc).__name__}: {exc}"
            log(f"拉起 Edge 失败：{ready_detail}")
    else:
        ready_detail = f"未找到 Edge 可执行文件（edge_exe={edge_exe!r}）"
        log(ready_detail)

    state.restarts += 1
    state.consecutive_failures = 0
    state.last_detail = detail
    save_state(state_path, state)
    log(f"重启结果：CDP {'已就绪' if ready else '仍未就绪'}")

    _alert(
        alert_fn,
        state,
        "⚠️ 美团监控：调试 Edge 已自动重启\n"
        f"原因：CDP 探测连续 {failures} 次失败（{detail}）\n"
        f"处理：结束 {len(killed)} 个调试 Edge 进程，重新拉起（pid={launched or '—'}）\n"
        f"结果：CDP {'已就绪' if ready else '仍未就绪'}（{ready_detail}）\n"
        f"时间：{state.last_check}",
    )
    return {
        "action": "restarted",
        "detail": detail,
        "killed": killed,
        "launched": launched,
        "cdp_ready": ready,
        "detail_after": ready_detail,
        "restarts": state.restarts,
    }
