"""Command-line entry point for the Meituan order monitor.

Sub-commands:
  import  Read a saved order-list JSON response from disk and archive it.
  pull    Connect to a locally logged-in browser and capture a live response.
  audit   Compare the scheduled window against raw/ captures and the run log.
  watch   Pull on a fixed interval, self-healing and alerting on failures.

``import`` needs only the standard library. ``pull`` / ``watch`` lazily import
the CDP bridge (which requires ``playwright``) and the push layer (which
requires ``requests``) so the rest of the tool stays usable without those deps.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from .normalize import summarize_orders
from .storage import save_raw, save_summary

WEBHOOK_FILE = Path("config/notify")


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _push(root: Path, orders, store_notify: bool = True) -> None:
    """Best-effort Enterprise WeChat push; never raises on missing deps/config."""
    if not orders:
        return
    try:
        from . import notify
    except ImportError as exc:
        print(f"⚠️ 推送依赖缺失（需 requests），跳过推送：{exc}", file=sys.stderr)
        return
    pushed, skipped = notify.process_notifications(
        orders,
        Path(root) / WEBHOOK_FILE,
        root=root,
        store_notify=store_notify,
        on_event=print,
    )
    print(f"企业微信推送：成功 {pushed} 笔")

def cmd_import(
    root: Path, source: str, no_notify: bool = False, no_store_notify: bool = False
) -> int:
    source_path = Path(source)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取响应文件：{exc}", file=sys.stderr)
        return 2

    order_list = payload.get("data", {}).get("orderList")
    if not order_list:
        print("错误：响应不含订单列表（data.orderList 为空或缺失），已跳过写入。")
        return 2

    # Archive the raw response first, unconditionally, then best-effort summary
    # (a malformed order is skipped, not fatal to the whole import).
    raw_path = save_raw(root, payload)
    summary = summarize_orders(payload)
    summary_path = save_summary(root, summary)
    print(f"原始数据：{raw_path}")
    print(f"订单摘要：{summary_path}（{len(summary)} 笔）")
    if not no_notify:
        _push(root, summary, store_notify=not no_store_notify)
    return 0


def cmd_audit(root: Path, day: str | None = None) -> int:
    """Report minutes within the monitoring window that produced no capture.

    Cross-references ``raw/`` (one file per successful capture) with
    ``logs/pull-YYYY-MM-DD.log`` (one start/end line per scheduled run) so a
    missing minute can be traced to "task never fired", "run failed" (with the
    printed reason) or "run still in flight".
    """
    from .report import audit, format_report

    target = None
    if day:
        try:
            target = date.fromisoformat(day)
        except ValueError:
            print(f"日期格式无效（需要 YYYY-MM-DD）：{day}", file=sys.stderr)
            return 2
    report = audit(root, target)
    print(format_report(report))
    return 0 if report.ok else 1


def _pull_event_printer():
    """Print bridge events, prefixing a warning marker unless the event already
    carries its own level marker (the per-round capture tally is informational and
    must not look like a problem in the run log)."""

    def _print(message: str) -> None:
        marker = "" if message[:1] in ("ℹ", "✅", "⚠") else "⚠️ "
        print(f"{marker}{message}", file=sys.stderr)

    return _print


def cmd_pull(
    root: Path,
    cdp_url: str,
    timeout: int,
    no_notify: bool = False,
    no_store_notify: bool = False,
    retries: int = 2,
) -> int:
    # Detect the CDP dependency at the CLI layer so a missing ``playwright``
    # yields a clear install hint instead of the generic
    # "拉取失败：No module named 'playwright'".
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "缺少依赖 playwright，请先安装：pip install playwright\n"
            "（无需 playwright install chromium，因为连接的是本机已运行的浏览器）",
            file=sys.stderr,
        )
        return 3

    try:
        from . import bridge
    except ImportError as exc:
        print(f"⚠️ 桥接模块加载失败：{exc}", file=sys.stderr)
        return 3

    # `pull` connects to the user's live, logged-in browser via CDP and reuses
    # its session, so no saved request template or auth file is required (the
    # dynamic mtgsig is produced by the browser, not read from disk).
    #
    # `retries` is the number of extra attempts after a *retryable* failure
    # (stuck/blank page); each retry recovers the page first (reload -> hard
    # reload -> re-navigate) and then waits until it is genuinely ready again.
    try:
        raw_path, summary_path = bridge.pull_order_list(
            root,
            cdp_url=cdp_url,
            timeout=timeout,
            max_attempts=max(1, retries + 1),
            on_event=_pull_event_printer(),
        )
    except ImportError:
        # Belt-and-suspenders in case playwright is missing deeper down. The
        # cost is that the bridge's retry wrapper turns a deep ImportError into
        # a BridgeError, so check the cause chain too (see bridge.pull_order_list).
        print(
            "缺少依赖 playwright，请先安装：pip install playwright\n"
            "（无需 playwright install chromium，因为连接的是本机已运行的浏览器）",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # bridge raises user-facing messages
        if any(
            isinstance(cause, ImportError)
            for cause in (exc, getattr(exc, "__cause__", None))
        ):
            print(
                "缺少依赖 playwright，请先安装：pip install playwright\n"
                "（无需 playwright install chromium，因为连接的是本机已运行的浏览器）",
                file=sys.stderr,
            )
            return 3
        print(f"拉取失败：{exc}", file=sys.stderr)
        return 1

    try:
        orders = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    except Exception:
        orders = []
    count = len(orders)
    print(f"原始数据：{raw_path}")
    print(f"订单摘要：{summary_path}（{count} 笔）")
    if not no_notify:
        _push(root, orders, store_notify=not no_store_notify)
    return 0


def run_logged(root: Path, run_fn, *, now_fn=None) -> int:
    """Run ``run_fn`` and append a start/end block to ``logs/pull-YYYY-MM-DD.log``.

    Logging lives here rather than in the ``.cmd`` wrapper on purpose: cmd's
    ``>>`` redirection takes the file exclusively enough that a run overlapping
    a long one could not open the shared log and silently did nothing (observed
    2026-09-18 19:55:01 under the task's Parallel policy — the tick "ran",
    returned 0 and left neither a log line nor a capture). Python's append mode
    tolerates concurrent writers, so every overlapping attempt is recorded.

    The captured output is echoed to the console as well, and the wrapped
    function's exit code is returned unchanged so the scheduled task reports it.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    now_fn = now_fn or datetime.now
    logs_dir = Path(root) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    start = now_fn()
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        code = run_fn()
    end = now_fn()

    lines = [f"[{start:%Y-%m-%d %H:%M:%S}] --- pull start ---"]
    output = buffer.getvalue().strip("\n")
    if output:
        lines.extend(output.splitlines())
    lines.append(f"[{end:%Y-%m-%d %H:%M:%S}] --- pull end (exit={code}) ---")

    log_path = logs_dir / f"pull-{start:%Y-%m-%d}.log"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    if output:
        print(output)
    return code


def cmd_pull_logged(
    root: Path,
    cdp_url: str,
    timeout: int,
    no_notify: bool = False,
    no_store_notify: bool = False,
    retries: int = 2,
) -> int:
    """``pull`` with the scheduled task's per-run log block (see run_logged)."""

    def _run() -> int:
        return cmd_pull(
            root,
            cdp_url,
            timeout,
            no_notify,
            no_store_notify,
            retries=retries,
        )

    return run_logged(root, _run)


def _load_pull_dependencies():
    """Import playwright + bridge for `watch`, or return ``None`` on failure."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "缺少依赖 playwright，请先安装：pip install playwright\n"
            "（无需 playwright install chromium，因为连接的是本机已运行的浏览器）",
            file=sys.stderr,
        )
        return None
    try:
        from . import bridge
    except ImportError as exc:
        print(f"⚠️ 桥接模块加载失败：{exc}", file=sys.stderr)
        return None
    return bridge


def cmd_watch(
    root: Path,
    cdp_url: str,
    timeout: int,
    interval: int,
    max_failures: int,
    alert_after: int,
    retries: int,
    no_notify: bool = False,
    no_store_notify: bool = False,
    once: bool = False,
) -> int:
    """Unattended loop: pull every ``interval`` seconds, self-healing per pull.

    The loop keeps running through individual failures; only ``max_failures``
    *consecutive* failures stop it (0 = never stop), which is what a supervisor
    or an on-call human needs in order to intervene.
    """
    bridge = _load_pull_dependencies()
    if bridge is None:
        return 3
    try:
        from . import watch as watch_mod
    except ImportError as exc:
        print(f"⚠️ 守护模块加载失败：{exc}", file=sys.stderr)
        return 3

    def pull_fn():
        return bridge.pull_order_list(
            root,
            cdp_url=cdp_url,
            timeout=timeout,
            max_attempts=max(1, retries + 1),
            on_event=watch_mod.log,
        )

    def notify_fn(summary_path):
        try:
            orders = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        except Exception:
            orders = []
        _push(root, orders, store_notify=not no_store_notify)

    alert_fn = None
    if alert_after:
        try:
            alert_fn = watch_mod.make_webhook_alerter(root, WEBHOOK_FILE)
        except ImportError as exc:
            print(f"⚠️ 告警依赖缺失（需 requests），已关闭告警：{exc}", file=sys.stderr)

    state = watch_mod.run_forever(
        pull_fn,
        interval=interval,
        max_failures=max_failures,
        alert_after=alert_after,
        notify_fn=None if no_notify else notify_fn,
        alert_fn=alert_fn,
        once=once,
    )
    print(watch_mod.summarise(state))
    # A watch that exited because failures piled up must not look like success.
    if max_failures and state.consecutive_failures >= max_failures:
        return 1
    return 0


def cmd_edge_watch(
    cdp_url: str,
    profile_dir: str,
    edge_exe: str,
    threshold: int,
    probe_timeout: float,
    startup_timeout: float,
    root: Path,
    no_alert: bool = False,
) -> int:
    """Probe CDP; after ``threshold`` consecutive failures restart the monitor Edge.

    Complements the per-page self-healing in ``bridge``: a *wedged browser
    process* keeps port 9222 listening while every CDP handshake times out, so
    no pull can succeed until someone restarts the browser. This check runs on a
    schedule, restarts only the Edge instance bound to the monitor's own
    profile, and alerts the main WeChat group.
    """
    from . import edge_watch as ew

    alert_fn = None
    if not no_alert:
        try:
            from .watch import make_webhook_alerter

            alert_fn = make_webhook_alerter(root, WEBHOOK_FILE)
        except ImportError as exc:
            print(f"⚠️ 告警依赖缺失（需 requests），已关闭告警：{exc}", file=sys.stderr)

    result = ew.run_check(
        cdp_url=cdp_url,
        profile_dir=profile_dir,
        edge_exe=edge_exe or ew.find_edge_exe(),
        state_path=Path(root) / ew.STATE_FILE,
        threshold=threshold,
        probe_timeout=probe_timeout,
        startup_timeout=startup_timeout,
        alert_fn=alert_fn,
    )
    # A restart that did not bring CDP back is a failure the supervisor should see.
    if result.get("action") == "restarted" and not result.get("cdp_ready"):
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mt-monitor", description="美团闪购商家端订单采集工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="从本地 JSON 文件导入一次接口响应")
    p_import.add_argument("source", help="接口原始响应 JSON 文件路径")
    p_import.add_argument(
        "--root", default=None, help="项目根目录（默认自动推断）"
    )
    p_import.add_argument(
        "--no-notify", action="store_true", help="跳过企业微信推送"
    )
    p_import.add_argument(
        "--no-store-notify",
        action="store_true",
        help="跳过门店群推送（主推送不受影响）",
    )

    p_pull = sub.add_parser("pull", help="通过本机已登录浏览器实时拉取待接单")
    p_pull.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="本机浏览器 CDP 调试地址",
    )
    p_pull.add_argument(
        "--timeout", type=int, default=30, help="捕获订单响应的超时秒数"
    )
    p_pull.add_argument(
        "--retries",
        type=int,
        default=2,
        help="页面卡死时的刷新重试次数（默认 2，每次重试前自动刷新页面）",
    )
    p_pull.add_argument(
        "--root", default=None, help="项目根目录（默认自动推断）"
    )
    p_pull.add_argument(
        "--no-notify", action="store_true", help="跳过企业微信推送"
    )
    p_pull.add_argument(
        "--no-store-notify",
        action="store_true",
        help="跳过门店群推送（主推送不受影响）",
    )

    p_audit = sub.add_parser(
        "audit", help="核对每分钟抓取是否都执行（对照 logs/pull-*.log 定位原因）"
    )
    p_audit.add_argument("--date", default=None, help="日期 YYYY-MM-DD，默认今天")
    p_audit.add_argument("--root", default=None, help="项目根目录（默认自动推断）")

    p_watch = sub.add_parser(
        "watch", help="常驻守护：定时拉取，页面卡死自动刷新重试，连续失败告警"
    )
    p_watch.add_argument(
        "--interval", type=int, default=60, help="两次拉取的间隔秒数（默认 60）"
    )
    p_watch.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="本机浏览器 CDP 调试地址",
    )
    p_watch.add_argument(
        "--timeout", type=int, default=30, help="捕获订单响应的超时秒数"
    )
    p_watch.add_argument(
        "--retries",
        type=int,
        default=2,
        help="每轮拉取内部的自愈重试次数（默认 2）",
    )
    p_watch.add_argument(
        "--max-failures",
        type=int,
        default=0,
        help="连续失败多少次后退出（默认 0 = 永不退出，一直守着）",
    )
    p_watch.add_argument(
        "--alert-after",
        type=int,
        default=3,
        help="连续失败多少次后推送企微告警（默认 3，0 = 关闭告警）",
    )
    p_watch.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮就退出（用于自检/launchd 定时拉起）",
    )
    p_watch.add_argument(
        "--root", default=None, help="项目根目录（默认自动推断）"
    )
    p_watch.add_argument(
        "--no-notify", action="store_true", help="跳过企业微信推送"
    )
    p_watch.add_argument(
        "--no-store-notify",
        action="store_true",
        help="跳过门店群推送（主推送不受影响）",
    )

    p_edge = sub.add_parser(
        "edge-watch",
        help="调试 Edge 看门狗：CDP 连续探测失败则自动重启浏览器并告警",
    )
    p_edge.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="本机浏览器 CDP 调试地址",
    )
    p_edge.add_argument(
        "--profile-dir",
        default=r"C:\tmp\mt-monitor-edge",
        help="监控专用 Edge 的 user-data-dir（只重启用它的实例）",
    )
    p_edge.add_argument(
        "--edge-exe",
        default="",
        help="Edge 可执行文件路径（默认自动探测常见安装位置）",
    )
    p_edge.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="连续探测失败多少次后重启浏览器（默认 3）",
    )
    p_edge.add_argument(
        "--probe-timeout",
        type=float,
        default=5.0,
        help="单次 CDP 探测超时秒数（默认 5）",
    )
    p_edge.add_argument(
        "--startup-timeout",
        type=float,
        default=60.0,
        help="重启后等待 CDP 就绪的秒数（默认 60）",
    )
    p_edge.add_argument(
        "--no-alert", action="store_true", help="重启后不推送企微告警"
    )
    p_edge.add_argument("--root", default=None, help="项目根目录（默认自动推断）")

    p_pull_logged = sub.add_parser(
        "pull-logged",
        help="计划任务用：执行一次 pull，并把开始/结束/退出码写入 logs/pull-*.log",
    )
    p_pull_logged.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="本机浏览器 CDP 调试地址",
    )
    p_pull_logged.add_argument(
        "--timeout", type=int, default=30, help="捕获订单响应的超时秒数"
    )
    p_pull_logged.add_argument(
        "--retries",
        type=int,
        default=2,
        help="页面卡死时的刷新重试次数（默认 2）",
    )
    p_pull_logged.add_argument("--root", default=None, help="项目根目录（默认自动推断）")
    p_pull_logged.add_argument(
        "--no-notify", action="store_true", help="跳过企业微信推送"
    )
    p_pull_logged.add_argument(
        "--no-store-notify",
        action="store_true",
        help="跳过门店群推送（主推送不受影响）",
    )

    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else _default_root()

    if args.command == "import":
        return cmd_import(root, args.source, args.no_notify, args.no_store_notify)
    if args.command == "pull":
        return cmd_pull(
            root,
            args.cdp,
            args.timeout,
            args.no_notify,
            args.no_store_notify,
            retries=args.retries,
        )
    if args.command == "watch":
        return cmd_watch(
            root,
            args.cdp,
            args.timeout,
            args.interval,
            args.max_failures,
            args.alert_after,
            args.retries,
            no_notify=args.no_notify,
            no_store_notify=args.no_store_notify,
            once=args.once,
        )
    if args.command == "audit":
        return cmd_audit(root, args.date)
    if args.command == "pull-logged":
        return cmd_pull_logged(
            root,
            args.cdp,
            args.timeout,
            args.no_notify,
            args.no_store_notify,
            retries=args.retries,
        )
    if args.command == "edge-watch":
        return cmd_edge_watch(
            args.cdp,
            args.profile_dir,
            args.edge_exe,
            args.threshold,
            args.probe_timeout,
            args.startup_timeout,
            root,
            no_alert=args.no_alert,
        )

    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
