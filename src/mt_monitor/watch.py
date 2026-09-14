"""Unattended watch loop: pull the pending-order list on a fixed interval.

Why a loop instead of cron:
  A stuck merchant tab (blank page / dead renderer) is *self-healed* by the
  bridge, and a healed pull must still deliver its orders. Running the loop here
  keeps the retry state in one process and lets repeated failures escalate to an
  Enterprise WeChat alert and, past ``max_failures``, to a deliberate exit so a
  supervisor (launchd) or a human can intervene.

Design:
  * The loop holds no browser state — every cycle calls ``pull_fn``, which
    reconnects over CDP. A browser restart between cycles is therefore fine.
  * Only *consecutive* failures count, so one bad cycle never stops the watch.
  * Alerts go to the main group webhook (``config/notify`` / ``QYWECHAT_WEBHOOK``
    via :mod:`notify`); they never raise — an unreachable webhook must not kill
    a running watch.

``pull_fn`` / ``notify_fn`` / ``alert_fn`` are injected so the loop can be
tested without a browser, a network or a real clock.
"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

# Start alerting after this many consecutive failures (0 disables alerts).
DEFAULT_ALERT_AFTER = 3
# 0 == keep watching forever.
DEFAULT_MAX_FAILURES = 0
DEFAULT_INTERVAL = 60


def log(message: str) -> None:
    """Timestamped, immediately flushed progress line."""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


@dataclass
class WatchState:
    """Mutable counters shared by the loop and its callbacks."""

    cycles: int = 0
    consecutive_failures: int = 0
    total_failures: int = 0
    last_error: str | None = None
    alerts_sent: int = 0
    stop_reason: str | None = None
    history: list = field(default_factory=list)


class _Stopper:
    """Turn SIGINT/SIGTERM into a clean loop exit."""

    def __init__(self, state: WatchState):
        self.state = state
        self._previous = {}

    def __enter__(self):
        def handler(signum, _frame):
            name = "SIGINT" if signum == signal.SIGINT else f"信号 {signum}"
            self.state.stop_reason = f"收到 {name}，已停止"
            log(self.state.stop_reason)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread / unsupported platform
        return self

    def __exit__(self, *exc_info):
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        return False


def run_forever(
    pull_fn,
    interval: int = DEFAULT_INTERVAL,
    max_failures: int = DEFAULT_MAX_FAILURES,
    alert_after: int = DEFAULT_ALERT_AFTER,
    notify_fn=None,
    alert_fn=None,
    sleep_fn=time.sleep,
    state: WatchState | None = None,
    once: bool = False,
) -> WatchState:
    """Run ``pull_fn`` every ``interval`` seconds until stopped.

    ``pull_fn()`` must return the ``(raw_path, summary_path)`` pair produced by
    :func:`bridge.pull_order_list`; ``notify_fn(summary_path)`` pushes the
    captured orders. Both may raise — failures are counted, not fatal.

    Returns the final :class:`WatchState` (``stop_reason`` explains the exit).
    """
    state = state if state is not None else WatchState()
    interval = max(1, int(interval))

    def _alert(message: str) -> None:
        if alert_fn is None:
            return
        try:
            alert_fn(message)
            state.alerts_sent += 1
        except Exception as exc:  # noqa: BLE001 - alerts must never kill watch
            log(f"告警发送失败（已忽略）：{type(exc).__name__}: {exc}")

    log(
        f"watch 启动：每 {interval}s 拉取一次"
        + (f"，连续失败 {max_failures} 次后退出" if max_failures else "，永不退出")
        + (f"，连续失败 {alert_after} 次后告警" if alert_after else "")
    )

    with _Stopper(state):
        while state.stop_reason is None:
            state.cycles += 1
            try:
                raw_path, summary_path = pull_fn()
                state.consecutive_failures = 0
                state.last_error = None
                log(f"第 {state.cycles} 次拉取成功：{summary_path}")
                if notify_fn is not None:
                    try:
                        notify_fn(summary_path)
                    except Exception as exc:  # noqa: BLE001
                        log(f"推送失败（不影响采集）：{type(exc).__name__}: {exc}")
            except KeyboardInterrupt:
                state.stop_reason = "收到中断，已停止"
                log(state.stop_reason)
                break
            except StopIteration:
                # Never swallow it: an exhausted iterator would otherwise turn
                # into an infinite loop of "failures" that nobody can explain.
                raise
            except Exception as exc:  # noqa: BLE001 - any pull failure is counted
                state.consecutive_failures += 1
                state.total_failures += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                state.history.append(state.last_error)
                log(
                    f"第 {state.cycles} 次拉取失败（连续 {state.consecutive_failures} "
                    f"次）：{state.last_error}"
                )
                # Escalation: one alert per threshold crossing, and a distinct
                # exit alert *instead of* the routine one when giving up —
                # sending both would double-notify on the final attempt only.
                if max_failures and state.consecutive_failures >= max_failures:
                    state.stop_reason = (
                        f"连续失败 {state.consecutive_failures} 次，达到上限，已退出"
                    )
                    log(state.stop_reason)
                    _alert(
                        f"🛑 订单监控已连续失败 {state.consecutive_failures} 次，"
                        f"守护进程退出（{state.last_error}）。请人工处理后重启。"
                    )
                    break
                if alert_after and state.consecutive_failures >= alert_after:
                    _alert(
                        f"⚠️ 订单监控连续 {state.consecutive_failures} 次采集失败\n"
                        f"最近错误：{state.last_error}\n"
                        "请检查本机 Edge 是否已登录美团、是否卡死。"
                    )

            if once:
                state.stop_reason = state.stop_reason or "单次模式完成"
                break
            if state.stop_reason is not None:
                break
            try:
                sleep_fn(interval)
            except KeyboardInterrupt:
                state.stop_reason = "收到中断，已停止"
                log(state.stop_reason)
                break

    log(
        f"watch 结束：共 {state.cycles} 次循环，成功 "
        f"{state.cycles - state.total_failures} 次，失败 {state.total_failures} 次"
        + (f"（{state.stop_reason}）" if state.stop_reason else "")
    )
    return state


def make_webhook_alerter(root, path) -> Callable[[str], None]:
    """Build an alert callback that pushes to the main group webhook.

    Imported lazily so a missing ``requests`` disables alerts instead of
    breaking the watch loop.
    """
    from .wechat_webhook import WechatWebhookClient, load_webhook_url

    def _alert(message: str) -> None:
        url = load_webhook_url(Path(root) / path)
        WechatWebhookClient(url).send_text(message)

    return _alert


def summarise(state: WatchState) -> str:
    """One-line human summary of a finished watch run."""
    return (
        f"循环 {state.cycles} 次，失败 {state.total_failures} 次，"
        f"告警 {state.alerts_sent} 次"
        + (f"，最近错误：{state.last_error}" if state.last_error else "")
    )


__all__ = [
    "DEFAULT_ALERT_AFTER",
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_FAILURES",
    "WatchState",
    "log",
    "make_webhook_alerter",
    "run_forever",
    "summarise",
]
