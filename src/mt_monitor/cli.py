"""Command-line entry point for the Meituan order monitor.

Sub-commands:
  import  Read a saved order-list JSON response from disk and archive it.
  pull    Connect to a locally logged-in browser and capture a live response.

``import`` needs only the standard library. ``pull`` lazily imports the CDP
bridge (which requires ``playwright``) and the push layer (which requires
``requests``) so the rest of the tool stays usable without those deps.
"""
from __future__ import annotations

import argparse
import json
import sys
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
        orders, Path(root) / WEBHOOK_FILE, root=root, store_notify=store_notify
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


def cmd_pull(
    root: Path,
    cdp_url: str,
    timeout: int,
    no_notify: bool = False,
    no_store_notify: bool = False,
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
    try:
        raw_path, summary_path = bridge.pull_order_list(
            root, cdp_url=cdp_url, timeout=timeout
        )
    except ImportError:
        # Belt-and-suspenders in case playwright is missing deeper down.
        print(
            "缺少依赖 playwright，请先安装：pip install playwright\n"
            "（无需 playwright install chromium，因为连接的是本机已运行的浏览器）",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # bridge raises user-facing messages
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

    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else _default_root()

    if args.command == "import":
        return cmd_import(root, args.source, args.no_notify, args.no_store_notify)
    if args.command == "pull":
        return cmd_pull(
            root, args.cdp, args.timeout, args.no_notify, args.no_store_notify
        )

    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
