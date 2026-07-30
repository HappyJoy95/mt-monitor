"""Push captured Meituan orders to an Enterprise WeChat robot.

Mirrors the design in jd-monitor/jd_monitor/notifications.py: a thin
formatting layer over :mod:`wechat_webhook`. Orders are summaries produced
by :func:`normalize.summarize_orders` (each carries ``order_id``, ``status``,
``store``, ``items``...). Pushing fires for every captured order by default;
``dedup`` can optionally skip ``order_id`` values already pushed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

from .wechat_webhook import WechatWebhookClient, WechatWebhookError, load_webhook_url

SEEN_FILE = Path("data/seen_orders.json")

# Source label prepended to every pushed message so the group can tell which
# monitor sent it (e.g. when several shop bots share one WeChat group).
SOURCE_LABEL = "美团闪购"


def format_notification(order: dict, status: str) -> str:
    """Render one order as a plain-text message for the robot.

    Always surfaces the three fields the business cares about: order id,
    status and store, plus a short product list. The message is prefixed
    with ``SOURCE_LABEL`` so the destination group knows it came from the
    Meituan monitor.
    """
    items = order.get("items", []) or []
    names = "、".join(
        "{}x{}".format(it.get("name", "商品"), it.get("quantity", 1))
        for it in items[:5]
        if isinstance(it, dict)
    ) or "商品信息待确认"
    return "【{}】{}\n订单号：{}\n门店：{}\n商品：{}".format(
        SOURCE_LABEL,
        status,
        order.get("order_id", ""),
        order.get("store", ""),
        names,
    )


def _load_seen(root: Path) -> set:
    p = Path(root) / SEEN_FILE
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(root: Path, seen: set) -> None:
    p = Path(root) / SEEN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def process_notifications(
    orders: Iterable[dict],
    webhook_path: Path | str,
    *,
    root: Path | str = ".",
    dedup: bool = False,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Push each order as a text message. Returns ``(pushed, skipped)``.

    ``webhook_path`` points to a file holding the robot URL (see
    :func:`wechat_webhook.load_webhook_url`); it is used only as a fallback —
    the ``QYWECHAT_WEBHOOK`` environment variable takes precedence. ``dedup``
    defaults to False, so every captured order is pushed on each run (repeats
    are harmless for the business). When set to True, orders whose ``order_id``
    was already pushed are skipped. ``dry_run`` prints the message instead of
    sending and never persists the seen-set (useful for local verification
    without a real webhook).
    """
    root = Path(root)
    orders = list(orders)
    seen = _load_seen(root) if dedup else set()
    pushed = 0
    skipped = 0

    client = None
    if not dry_run:
        try:
            client = WechatWebhookClient(load_webhook_url(Path(webhook_path)))
        except WechatWebhookError as exc:
            if exc.code == "not_configured":
                print(
                    "⚠️ 未配置企业微信 webhook（未设置环境变量 QYWECHAT_WEBHOOK，"
                    "且 config/notify 不存在），跳过推送。",
                    file=sys.stderr,
                )
            else:
                print(
                    f"⚠️ 企业微信 webhook 配置无效，跳过推送：{exc}", file=sys.stderr
                )
            return 0, 0

    for order in orders:
        oid = str(order.get("order_id", ""))
        if not oid:
            continue
        if dedup and oid in seen:
            skipped += 1
            continue
        status = order.get("status") or "订单提醒"
        text = format_notification(order, status)
        if dry_run:
            print(f"[dry-run] 将推送订单 {oid}:\n{text}\n")
            pushed += 1
            continue
        try:
            client.send_text(text)
            pushed += 1
            seen.add(oid)
        except WechatWebhookError as exc:
            print(f"⚠️ 推送订单 {oid} 失败：{exc}", file=sys.stderr)

    if dedup and not dry_run:
        _save_seen(root, seen)
    return pushed, skipped
