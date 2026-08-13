"""Push captured Meituan orders to an Enterprise WeChat robot.

Mirrors the design in jd-monitor/jd_monitor/notifications.py: a thin
formatting layer over :mod:`wechat_webhook`. Orders are summaries produced
by :func:`normalize.summarize_orders` (each carries ``order_id``, ``status``,
``store``, ``items``...). Pushing fires for every captured order by default;
``dedup`` can optionally skip ``order_id`` values already pushed.

Supports optional per-store webhook routing via ``config/store_webhooks.json``:
when configured, each order is also pushed to its store's group webhook if the
current time falls within the store's business hours.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Iterable

from .wechat_webhook import WechatWebhookClient, WechatWebhookError, load_webhook_url

SEEN_FILE = Path("data/seen_orders.json")
STORE_WEBHOOKS_FILE = Path("config/store_webhooks.json")

# Source label prepended to every pushed message so the group can tell which
# monitor sent it (e.g. when several shop bots share one WeChat group).
SOURCE_LABEL = "美团闪购"


def _load_store_webhooks(root: Path) -> list[dict]:
    """Load per-store webhook mapping from config/store_webhooks.json.

    Returns a list of dicts, each with keys: 门店名, 营业开始时间, 营业结束时间, webhook.
    Returns an empty list if the file is missing or malformed.
    """
    p = root / STORE_WEBHOOKS_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _is_within_business_hours(start_str: str, end_str: str) -> bool:
    """Check if the current time is within the given business hours.

    Supports times that cross midnight (e.g. 22:00-06:00).
    """
    now = datetime.now().time()
    try:
        start = time.fromisoformat(start_str)
        end = time.fromisoformat(end_str)
    except (ValueError, TypeError):
        return True  # malformed config → don't block push

    if start <= end:
        return start <= now <= end
    # crosses midnight
    return now >= start or now <= end


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


def format_store_deployment_message(
    store_name: str, start_time: str, end_time: str
) -> str:
    """Format a store deployment confirmation message (scheme B)."""
    return (
        f"【{SOURCE_LABEL}】门店推送配置确认\n"
        "————————————\n"
        f"门店名称：{store_name}\n"
        f"营业时间：{start_time} ~ {end_time}\n"
        f"推送平台：{SOURCE_LABEL}订单监控\n"
        "推送频率：每分钟一次\n"
        "推送状态：待接单\n"
        "————————————\n"
        "状态：配置完成，正常推送中"
    )


def send_store_deployment_message(
    webhook_url: str,
    store_name: str,
    start_time: str,
    end_time: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Send deployment confirmation to a store's webhook. Returns True on success."""
    text = format_store_deployment_message(store_name, start_time, end_time)
    if dry_run:
        print(f"[dry-run] 门店部署确认消息：\n{text}\n")
        return True
    try:
        client = WechatWebhookClient(webhook_url)
        client.send_text(text)
        return True
    except WechatWebhookError as exc:
        print(f"⚠️ 门店部署确认推送失败（{store_name}）：{exc}", file=sys.stderr)
        return False


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
    store_notify: bool = True,
) -> tuple[int, int]:
    """Push each order as a text message. Returns ``(pushed, skipped)``.

    Pushes to the default webhook (config/notify or QYWECHAT_WEBHOOK env var)
    for every order. When ``store_notify`` is True and config/store_webhooks.json
    exists, each order whose store name matches an entry is also pushed to that
    store's group webhook — but only if the current time falls within the
    store's business hours.

    ``dedup`` defaults to False, so every captured order is pushed on each run.
    When set to True, orders whose ``order_id`` was already pushed are skipped.
    ``dry_run`` prints the message instead of sending and never persists the
    seen-set.
    """
    root = Path(root)
    orders = list(orders)
    seen = _load_seen(root) if dedup else set()
    pushed = 0
    skipped = 0

    default_client = None
    if not dry_run:
        try:
            default_client = WechatWebhookClient(load_webhook_url(Path(webhook_path)))
        except WechatWebhookError as exc:
            if exc.code == "not_configured":
                print(
                    "⚠️ 未配置企业微信 webhook（未设置环境变量 QYWECHAT_WEBHOOK，"
                    "且 config/notify 不存在），跳过主推送。",
                    file=sys.stderr,
                )
            else:
                print(
                    f"⚠️ 企业微信 webhook 配置无效，跳过主推送：{exc}", file=sys.stderr
                )

    store_map: dict[str, dict] = {}
    if store_notify:
        for entry in _load_store_webhooks(root):
            name = entry.get("门店名", "")
            if name:
                store_map[name] = entry

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
        order_pushed = False
        # Default webhook push
        if default_client is not None:
            try:
                default_client.send_text(text)
                seen.add(oid)
                order_pushed = True
            except WechatWebhookError as exc:
                print(f"⚠️ 主推送订单 {oid} 失败：{exc}", file=sys.stderr)
        # Store-specific webhook push
        store_name = order.get("store", "")
        entry = store_map.get(store_name)
        if entry:
            webhook_url = entry.get("webhook", "")
            start_time = entry.get("营业开始时间", "")
            end_time = entry.get("营业结束时间", "")
            if _is_within_business_hours(start_time, end_time):
                try:
                    client = WechatWebhookClient(webhook_url)
                    client.send_text(text)
                    seen.add(oid)
                    order_pushed = True
                except WechatWebhookError as exc:
                    print(
                        f"⚠️ 门店推送订单 {oid}（{store_name}）失败：{exc}",
                        file=sys.stderr,
                    )
            else:
                print(
                    f"ℹ️ 门店 {store_name} 当前非营业时间，跳过门店群推送。",
                    file=sys.stderr,
                )
        if order_pushed:
            pushed += 1

    if dedup and not dry_run:
        _save_seen(root, seen)
    return pushed, skipped
