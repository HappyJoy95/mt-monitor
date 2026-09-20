"""CDP bridge to a locally logged-in Meituan merchant browser.

The Meituan order API requires a dynamic ``mtgsig`` signature that cannot be
replayed from a static capture (a replayed request is rejected with 403 even
while the cookie is still valid). Instead of forging the signature, we connect
to a browser the user has already opened and logged in (Edge launched with
remote debugging), reuse its live session, and simply *capture* the
order-list responses the page makes on its own.

Launch Edge with:

    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
        --remote-debugging-port=9222 \
        --user-data-dir="/tmp/mt-monitor-edge"

Then open the merchant order page and run ``mt-monitor pull``.

How the list is fetched (verified against the live page):
  * A plain page reload only fires the count endpoint ``/order/list/count`` and
    the polling endpoint ``/order/list/interval`` (the latter returns per-state
    order *counts*, not the orders themselves).
  * The actual order list comes from ``/order/list/page/unprocessed`` (POST),
    and the page requests it **only when a state tab is (re)selected**. So we
    trigger the list by clicking the target tab rather than reloading.

Which tab, and which orders: we click ``TARGET_TAB`` ("进行中") because that
list carries the orders of every state, and :mod:`normalize` then keeps only the
statuses worth pushing ("待接单" / "待发起配送", the latter gated on its pickup
window). That strategy comes from the Windows side; this module only guarantees
the click actually produces a fresh response.

Self-healing (why this module reloads the page by itself):
  A long-lived merchant tab tends to get stuck — blank screen, spinner that
  never finishes, or a rendered page whose clicks no longer produce requests
  (useful only for unattended running via ``mt-monitor watch``). The symptom we
  can observe is always the same: no ``/order/list/page/unprocessed`` response
  arrives within the timeout. Since a manual refresh is the known cure, every
  *retryable* failure is answered with an escalating page recovery (soft reload
  -> cache-bypassing hard reload -> navigate to the order URL) followed by a
  wait until the page is really ready (document complete, ``hashframe`` iframe
  attached, state tab visible) and another capture attempt.

  Two failures are deliberately **not** retried:
    * the browser has bounced to the Meituan login page (no reload ever fixes
      an expired session — it needs a human to log in again);
    * the endpoint answered 401/403.

  Every failed pull also drops a diagnostic snapshot under
  ``data/last-pull-error.json`` (url / title / readyState / attempts / error) so
  a stuck page can be inspected afterwards.

Live-verified traps this module encodes (each one cost a silent failure):
  * the tab button class is CSS-Modules-hashed and the hash changes between
    Meituan builds (seen: ``tab-btn_c17``, later ``tab-btn_c17d4``), so the
    selector must match on a prefix;
  * the readiness probe must look for the *same* element the click locator
    resolves (``locator(sel, has_text=...)`` filters by text first), otherwise
    it inspects the strip's first tab ("全部") and never reports ready;
  * ``expect_response`` starts listening only once its ``with`` body has already
    clicked, and this page answers in ~0.05-0.3s, so the listener must be
    registered *before* the click;
  * a reload recreates the iframe, so the frame object must be re-resolved
    before clicking (otherwise: "Frame was detached").
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .normalize import summarize_orders
from .storage import save_raw, save_summary

ORDER_LIST_PATH = "/order/list/page/unprocessed"
ORDER_PAGE_MARKERS = ("shangoue.meituan.com", "orderbusiness")
ORDER_PAGE_URL = (
    "https://shangoue.meituan.com/#/page/orderbusiness#/order/unprocessed"
)
# Every state tab asks the *same* endpoint, so the URL cannot tell which tab
# asked — only the POST body's ``tag`` can. Matching on the URL alone let a late
# detour response pass as the target's: on 2026-09-20 11:42 the page had one
# 待发起配送 order under 进行中, the pull still recorded zero orders, and the
# order was never pushed.
TAB_TAGS = {
    "待接单": "order_new",
    "进行中": "order_processing",
    "待发配送": "order_prepLogistics",
    "全部": "order_all",
}
# Click "进行中" tab to get every live order, then filter by status in normalize.
TARGET_TAB = "进行中"
TAB_LABEL = TARGET_TAB
# Clicked to force a state change when TARGET_TAB is already selected, and also
# captured in its own right: re-clicking the active tab is a no-op, so no request
# would be fired.
FALLBACK_TAB = "待接单"
OPPOSITE_TAB_LABEL = FALLBACK_TAB
# Both tabs are captured on every pull because their order sets are *disjoint*:
# across 13354 September captures not one response ever mixed 待接单 with an
# in-progress status. 待接单 -> order_new holds the orders awaiting acceptance;
# 进行中 -> order_processing holds 待发起配送 and every later live state. A single
# response can therefore never carry both, while normalize needs both in order to
# push 待接单 + 待发起配送.
TARGET_TABS = (TARGET_TAB, FALLBACK_TAB)
# CSS Modules hash the class name, and the hash changes between Meituan builds
# (seen: `tab-btn_c17`, later `tab-btn_c17d4`). A prefix match on the *stable*
# part keeps working across those redeploys. The JS readiness probe is handed
# this same constant, so the probe and the click can never look for different
# elements.
TAB_SELECTOR = 'button[class*="tab-btn_"]'
# The selected tab carries `active_<hash>` (hash changes per build too).
ACTIVE_CLASS_PREFIX = "active"
# The order tabs / XHR live inside this iframe, not the top document.
ORDER_FRAME_NAME = "hashframe"
# Marker of the Meituan login flow; if the merchant page bounces here the
# session is gone and no amount of reloading helps.
LOGIN_URL_MARKERS = ("passport.meituan.com", "/login")
DIAGNOSTICS_FILE = Path("data/last-pull-error.json")
# Envelope codes that mean "log in again", not "page is stuck".
AUTH_FAILURE_CODES = (401, 403)
# How often the readiness wait re-checks whether a login wall appeared.
LOGIN_PROBE_INTERVAL = 1.5
# How long to wait for the response to the *detour* click (FALLBACK_TAB). The
# detour only exists to force a state change, so it is fine to move on quickly:
# live measurement is a hard "no response" on the already-active strip.
DETOUR_WAIT = 2.5
# A click that lands is answered in ~0.3s, so waiting 30s for a click that was
# swallowed just burns the round. Instead: wait briefly, click again (a few
# times), and let the round-level recovery handle a page that is truly stuck.
# Live 2026-09-20: Playwright's click intermittently timed out in its
# actionability check ("waiting for element to be visible, enabled and stable")
# while the SPA re-rendered the list, and a re-click fixed it.
CLICK_WAIT = 3.0
CLICK_ATTEMPTS = 3
CLICK_RETRY_PAUSE = 0.4
# Give the SPA a moment to finish rendering after a tab switch, otherwise the
# very next click can land mid-re-render and fire nothing.
TAB_SETTLE_PAUSE = 0.6

# The JS probe used for two purposes: (a) deciding the page is ready to click
# again after a recovery reload, (b) sitting in the click call itself so a
# stalled renderer shows up as a fast TimeoutError instead of hanging forever.
# Playwright passes a single JS argument, hence the array destructuring —
# passing two values would silently bind `selector` to the whole array and leave
# `label` undefined (a bug that cost a full readiness timeout on every pull).
_TAB_JS = """
([selector, label]) => {
    if (!window.document) return 'no-document';
    const els = Array.from(document.querySelectorAll(selector));
    if (!els.length) return 'no-tab';
    // Mirror the click locator exactly: `locator(selector, has_text=label)`
    // filters by text *first*, then takes the first match. Using
    // querySelector() here instead would inspect the first tab of the strip
    // ("全部") and report label-mismatch forever — which cost a full readiness
    // timeout on every single pull until it was caught live.
    const el = els.find(node => (node.textContent || '').includes(label));
    if (!el) return 'label-mismatch';
    if (typeof el.getBoundingClientRect !== 'function') return 'no-layout';
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return 'invisible';
    return true;
}
"""

_READY_JS = """
() => {
    if (!window.document) return false;
    const doc = document;
    if (doc.readyState !== 'complete') return false;
    const frame = doc.querySelector('iframe#hashframe');
    return !!(frame && typeof frame.getBoundingClientRect === 'function');
}
"""

# Login detection. A password box is proof; otherwise only a *login-wall*
# phrase counts. The page title is deliberately NOT matched: a real merchant
# page may legitimately contain "登录" (logout controls), and a false "needs
# login" would make the tool give up on a perfectly recoverable page — the
# expensive mistake. Missing a login page only costs one wasted refresh.
_LOGIN_JS = """
() => {
    try {
        const doc = document;
        if (doc.querySelector('input[type=password]')) return 'login';
        const body = doc.body ? (doc.body.innerText || '') : '';
        if (/立即登录|扫码登录|请先登录|账号登录/.test(body)) return 'login';
        const html = doc.documentElement
            ? (doc.documentElement.innerHTML || '') : '';
        if (html.length < 200) return 'blank';
        return 'ok';
    } catch (e) { return 'unknown'; }
}
"""


class BridgeError(RuntimeError):
    """A user-facing bridge failure (message is safe to print as-is)."""


class TransientBridgeError(BridgeError):
    """A failure that a page reload may plausibly cure (stuck/blank page)."""


class _ClickError(Exception):
    """Internal: the click itself failed (wrapped to classify it later)."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(str(cause))


class AuthExpiredError(BridgeError):
    """The session is gone; retrying/reloading cannot help, login is required."""


def _find_order_page(browser):
    """Return the first page showing the Meituan order business view."""
    for context in browser.contexts:
        for page in context.pages:
            url = page.url or ""
            if all(marker in url for marker in ORDER_PAGE_MARKERS):
                return page
    return None


def _read_payload(response):
    try:
        return response.json()
    except Exception:
        return None


def _click_tab(scope, label, timeout_ms: int = 5000) -> None:
    """Click the order-state tab with the given label to trigger a fresh list
    request. The merchant SPA fetches the order list only when its tab is
    (re)selected, not on a plain reload.

    ``scope`` is the page or (more reliably) the ``hashframe`` iframe, because
    the order tabs live inside that iframe rather than the top document. The tab
    buttons carry a hashed class (``tab-btn_<hash>``, see ``TAB_SELECTOR``) and a
    text of the form "<label> <count>" (e.g. "待接单 0"), so we match by class +
    text and use a real Playwright click (a synthetic ``el.click()`` can miss
    the SPA's event handler).

    Retries internally (short timeout each round) because Playwright's
    actionability checks (stable, receives events) intermittently fail on this
    heavy SPA; a quick retry is far cheaper than a full page recovery. Only
    ``TimeoutError`` (an actionability race) is retried — anything else is a
    real error and must reach the caller, which decides whether to recover.
    """
    last_exc = None
    for _ in range(3):
        try:
            scope.locator(TAB_SELECTOR, has_text=label).first.click(
                timeout=timeout_ms
            )
            return
        except TimeoutError as exc:
            last_exc = exc
            _sleep(0.5)
        except Exception:  # noqa: BLE001 - real errors bubble to the caller
            raise
    # Fallback: click any node inside the scope whose visible text starts with
    # the label (handles counts / badges appended after the label).
    try:
        scope.evaluate(
            """(label) => {
                const nodes = Array.from(document.querySelectorAll(
                    'button, a, li, div, span'));
                const el = nodes.find(
                    n => (n.textContent || '').trim().startsWith(label));
                if (el) el.click();
            }""",
            label,
        )
        return
    except Exception:  # noqa: BLE001 - keep the original click failure
        pass
    if last_exc is not None:
        raise last_exc


# --------------------------------------------------------------------------
# Time / readiness helpers (injectable so tests need no real browser or clock)
# --------------------------------------------------------------------------
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


def _probe_tab(scope, label: str):
    """Return the tab-readiness probe result (``True`` == clickable).

    A ``False``/``None`` result means the scope itself is not usable yet (the
    iframe was just recreated by a reload, or its execution context is gone).
    """
    try:
        return scope.evaluate(_TAB_JS, [TAB_SELECTOR, label])
    except Exception:  # noqa: BLE001 - detached frame / context destroyed
        return None


def _probe_active_tab(scope, label: str):
    """Return whether the tab carrying ``label`` is the selected one.

    ``True`` / ``False`` when the tabs are readable, ``None`` when they are not
    (so callers can fall back to the unconditional click dance).
    """
    try:
        return scope.evaluate(
            """([selector, label, activePrefix]) => {
                const els = Array.from(document.querySelectorAll(selector));
                const target = els.find(
                    el => (el.textContent || '').includes(label));
                if (!target) return null;
                const cls = (target.className || '').toString();
                return cls.split(/\\s+/).some(
                    name => name.startsWith(activePrefix));
            }""",
            [TAB_SELECTOR, label, ACTIVE_CLASS_PREFIX],
        )
    except Exception:  # noqa: BLE001 - detached frame / not ready
        return None


def _page_ready(page) -> bool:
    """True when the top document finished loading and ``hashframe`` is back."""
    try:
        if not page.evaluate(_READY_JS):
            return False
        frame = page.frame(name=ORDER_FRAME_NAME)
        if frame is None:
            return False
        # Any evaluation on the frame throws while its context is gone, which
        # is exactly the "iframe not usable yet" state we must not click into.
        frame.evaluate("() => true")
        return True
    except Exception:  # noqa: BLE001 - not ready (or navigation in flight)
        return False


def _wait_until_ready(page, frame, label: str, timeout: float, page_probe=None):
    """Poll until the page is loaded, ``hashframe`` exists and the target tab is
    clickable. Returns the resolved ``(page, frame)`` pair or ``None``.

    Clicking a tab that is not there yet is exactly how a capture attempt
    silently produces no request, so readiness is verified before every click.

    ``page_probe`` (usually :func:`_login_check`) is consulted periodically, not
    only at the end: an expired session shows a login wall that never grows an
    order iframe, and burning the whole readiness timeout just to report "page
    stuck" hides the actionable cause. Raises ``AuthExpiredError`` as soon as
    the probe says so, turning a ~45s stall into a ~3s clear failure.
    """
    deadline = _monotonic() + max(0.0, timeout)
    next_probe = _monotonic()
    while True:
        if _page_ready(page):
            scope = page.frame(name=ORDER_FRAME_NAME) or frame or page
            if _probe_tab(scope, label) is True:
                return page, scope
        now = _monotonic()
        if page_probe is not None and now >= next_probe:
            if page_probe(page) == "login":
                raise AuthExpiredError("等待订单页加载时检测到登录页，会话已失效")
            next_probe = now + LOGIN_PROBE_INTERVAL
        if now >= deadline:
            return None
        _sleep(0.5)


def _login_check(page):
    """Classify the top page as ``'ok'`` | ``'blank'`` | ``'login'`` | ``'unknown'``.

    Only explicit login signals count as ``'login'``: the top URL sitting in the
    login flow, a password field, or login-wall wording on the page. Being wrong
    towards ``'ok'`` merely costs one useless refresh, so ambiguous states
    (``'blank'`` / ``'unknown'``, e.g. mid-navigation) stay recoverable.
    """
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 - page closed mid-check
        return "unknown"
    if any(marker in url for marker in LOGIN_URL_MARKERS):
        return "login"
    try:
        signal = page.evaluate(_LOGIN_JS)
    except Exception:  # noqa: BLE001 - cannot tell => assume recoverable
        return "unknown"
    if not isinstance(signal, str):
        return "unknown"
    return signal


def _should_recover(page) -> bool:
    """False when the browser clearly needs a human (login page)."""
    return _login_check(page) != "login"


def _raise_auth_expired(root, page, detail, emit, why) -> None:
    """Record the failure scene and report a login failure. Never returns."""
    detail.update(
        {
            "stage": "capture",
            "error": why,
            "page": _diagnose(page),
            "hint": "登录态失效，需人工在 Edge 中重新登录",
        }
    )
    path = save_diagnostics(root, detail)
    if path is not None:
        emit(f"现场诊断：{path}")
    raise AuthExpiredError(
        "美团登录态已失效（页面在登录页），刷新无法恢复，"
        "请在 Edge 中重新登录后再试。"
    )


def _recovery_tier(page, label: str, recoveries: int) -> int:
    """Which recovery level to use for the next attempt.

    Soft/hard reloads re-run the *same* SPA bundle, so they cannot cure an order
    module that never rendered — on 2026-09-20 the iframe sat on "加载中..." with
    zero tab buttons, three rounds of soft/hard reloads failed, and a single
    navigate brought the tabs back. So when the target tab is not clickable, jump
    straight to the navigate tier; otherwise keep the gentle soft→hard ladder.
    """
    frame = page.frame(name=ORDER_FRAME_NAME)
    probe = _probe_tab(frame, label) if frame is not None else None
    if probe is not True:
        return 2
    return recoveries


def _reload_page(page, tier: int, timeout: float, on_event) -> bool:
    """Bring a stuck page back to life. Returns ``True`` when the reload itself
    went through (readiness is checked separately by ``_wait_until_ready``).

    ``tier`` is the escalation level (0 = soft reload, 1 = hard reload,
    >= 2 = navigate to the order URL).

    Escalation order — softest first:
      1. ``page.reload()`` — the plain F5 that fixes a blank/stuck SPA.
      2. CDP ``Page.reload(ignoreCache=True)`` — a hard reload for the case
         where a cached/broken bundle keeps the page white.
      3. ``goto(ORDER_PAGE_URL)`` — re-navigate when reload itself is refused
         (e.g. the tab ended up out of the SPA after an error interstitial).

    Never closes the page or the browser: the session belongs to the user.
    """
    if tier <= 0:
        try:
            page.reload(timeout=timeout * 1000, wait_until="domcontentloaded")
            on_event("已刷新页面（软刷新）")
            return True
        except Exception as exc:  # noqa: BLE001 - escalate to hard reload
            on_event(f"软刷新失败：{type(exc).__name__}")
    elif tier == 1:
        try:
            session = page.context.new_cdp_session(page)
            session.send("Page.reload", {"ignoreCache": True})
            on_event("已硬刷新页面（忽略缓存）")
            return True
        except Exception as exc:  # noqa: BLE001 - escalate to re-navigate
            on_event(f"硬刷新失败：{type(exc).__name__}")
    try:
        page.goto(
            ORDER_PAGE_URL, timeout=timeout * 1000, wait_until="domcontentloaded"
        )
        on_event("已重新导航到订单页")
        return True
    except Exception as exc:  # noqa: BLE001 - give up, caller reports
        on_event(f"重新导航失败：{type(exc).__name__}")
        return False


def _diagnose(page) -> dict:
    """Collect a small, safe snapshot of the page state for troubleshooting."""
    info = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": None,
        "title": None,
        "readyState": None,
        "hashframe": None,
    }
    try:
        info["url"] = page.url
    except Exception:  # noqa: BLE001
        pass
    try:
        info["title"] = page.title()
    except Exception:  # noqa: BLE001
        pass
    try:
        info["readyState"] = page.evaluate("() => document.readyState")
    except Exception:  # noqa: BLE001
        pass
    try:
        frame = page.frame(name=ORDER_FRAME_NAME)
        info["hashframe"] = frame is not None
        if frame is not None:
            info["tab"] = _probe_tab(frame, TARGET_TAB)
    except Exception:  # noqa: BLE001
        pass
    return info


def save_diagnostics(root, detail: dict) -> Path | None:
    """Best-effort write of the failure snapshot (never masks the real error)."""
    try:
        path = Path(root) / DIAGNOSTICS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except Exception:  # noqa: BLE001
        return None


def open_order_page(browser):
    """Open a fresh order page when none is found. Returns the page or ``None``.

    Only ever *adds* a tab; the user's existing tabs and session are untouched.
    """
    try:
        context = browser.contexts[0]
        page = context.new_page()
        page.goto(ORDER_PAGE_URL, timeout=30000, wait_until="domcontentloaded")
        return page
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------
def _tab_tag(label: str) -> str:
    """The request ``tag`` a tab click is expected to send ("" when unknown)."""
    return TAB_TAGS.get(label, "")


def _detour_label(label: str) -> str:
    """A *different* tab to click so the SPA refetches while ``label`` is active."""
    for candidate in TARGET_TABS:
        if candidate != label:
            return candidate
    return FALLBACK_TAB if label != FALLBACK_TAB else TARGET_TAB


def _order_key(order) -> str:
    """Stable identity of one raw order, for de-duplicating merged responses."""
    try:
        return str(
            json.loads(order.get("commonInfo", "{}")).get("wm_order_id_view", "")
        )
    except Exception:  # noqa: BLE001 - identity is best effort
        return ""


def _merge_payloads(payloads, labels) -> dict:
    """Union of several tab responses, de-duplicated by order id.

    ``待接单`` and ``进行中`` answer with disjoint order sets, so a pull has to
    merge them to see every order worth pushing. The first occurrence wins, and
    ``data.capturedTabs`` records which tabs contributed, so an archived raw file
    explains itself later. Pagination metadata is copied from the first response
    and is therefore only indicative.
    """
    orders = []
    seen = set()
    for payload in payloads:
        for order in (payload.get("data") or {}).get("orderList") or []:
            key = _order_key(order)
            if key:
                if key in seen:
                    continue
                seen.add(key)
            orders.append(order)

    base = dict(payloads[0]) if payloads else {"code": 0, "msg": "success"}
    data = dict(base.get("data") or {})
    data["orderList"] = orders
    data["capturedTabs"] = list(labels)
    base["data"] = data
    return base


def _capture_order_lists(page, frame, timeout: int, on_event, labels=TARGET_TABS) -> dict:
    """Capture one response per target tab and merge them into a single payload.

    Both tabs are needed because their order sets are disjoint (see
    ``TARGET_TABS``): a single response would silently drop either the 待接单 or
    the 待发起配送 orders — the bug that hid 待发起配送 for days. A failure on any
    tab fails the round, so the caller's recovery (reload + retry) can take over
    instead of archiving a quietly incomplete list.

    Per-tab counts are reported, because "订单摘要：0 笔" alone cannot tell a
    genuinely empty list from "the wrong tab answered" — the counters make the
    next such question answerable straight from the run log.
    """
    payloads = []
    captured_labels = []
    counts = []
    for label in labels:
        try:
            payload = _capture_order_list(page, frame, label, timeout, on_event)
        except TransientBridgeError as exc:
            raise TransientBridgeError(f"抓取「{label}」标签失败：{exc}") from exc
        payloads.append(payload)
        captured_labels.append(label)
        counts.append(len((payload.get("data") or {}).get("orderList") or []))

    merged = _merge_payloads(payloads, captured_labels)
    total = len((merged.get("data") or {}).get("orderList") or [])
    detail = " + ".join(
        f"{label} {count} 笔" for label, count in zip(captured_labels, counts)
    )
    dropped = sum(counts) - total
    on_event(
        f"ℹ️ 本轮抓取：{detail}（去重合并后 {total} 笔"
        + (f"，重复 {dropped} 笔" if dropped else "")
        + "）"
    )
    return merged


def _capture_order_list(page, frame, label: str, timeout: int, on_event):
    """Trigger a fresh order-list request and return the captured payload.

    The SPA fetches ``/order/list/page/unprocessed`` only when a state tab is
    (re)selected, and clicking an *already active* tab is a no-op (no request).

    Why a listener instead of ``expect_response``: the live page answers the
    click within ~0.3s, and ``expect_response`` only starts arming once the
    ``with`` body has already clicked — fast responses were being missed, which
    showed up as a full 20s stall per pull. The listener is registered *before*
    the first click, so nothing can slip through.

    Click strategy, decided from the live DOM (an optimization whose safety net
    is the extra click, not a failure):
      * target tab not active -> one click on it is enough;
      * target tab already active -> detour through ``FALLBACK_TAB`` first,
        otherwise the SPA would not refetch anything.
    A target click that produces no response is retried once through the
    fallback tab, so a swallowed click no longer costs a whole page reload.
    """
    captured = []
    wanted_tag = _tab_tag(label)

    def is_list_request(request) -> bool:
        # Filter on the *request* URL: Playwright's response.url can differ after
        # redirects, while request.url is exactly what the page asked for.
        return (
            getattr(request, "method", "") == "POST"
            and ORDER_LIST_PATH in (getattr(request, "url", "") or "")
        )

    def matches_tag(response) -> bool:
        # Every tab hits the same URL, so the request body's tag is the only way
        # to tell which tab a response answers. Without this check a slow detour
        # answer was accepted as the target's list (2026-09-20: a live
        # 待发起配送 order was dropped and never pushed).
        request = getattr(response, "request", None)
        if not is_list_request(request):
            return False
        body = getattr(request, "post_data", None) or ""
        return f"tag={wanted_tag}" in body

    def any_list_response(response) -> bool:
        return is_list_request(getattr(response, "request", None))

    listener = (
        lambda response: captured.append(response)
        if any_list_response(response)
        else None
    )
    page.on("response", listener)

    def wait_for(pred, wait_seconds):
        deadline = _monotonic() + wait_seconds
        while True:
            # Check before sleeping, and sleep in short slices: the live page
            # answers in ~0.3s, and a full-length sleep here would make every
            # pull wait out the whole budget even on the happy path.
            for index, response in enumerate(captured):
                if pred(response):
                    return captured.pop(index)
            if _monotonic() >= deadline:
                return None
            _sleep(min(0.2, max(0.0, deadline - _monotonic())))

    def click_and_wait(label_to_click: str, wait_seconds: float, pred=matches_tag):
        """Click ``label_to_click`` until a matching response arrives (or give up).

        Each attempt fences off older responses, clicks, and waits only briefly:
        an answer that is going to come arrives in ~0.3s, and a longer wait would
        just delay the next click. Re-clicking is what recovers the live case
        where the click landed while the SPA re-rendered the tab strip.
        """
        budget = min(wait_seconds, CLICK_WAIT)
        for _attempt in range(CLICK_ATTEMPTS):
            del captured[:]
            try:
                _click_tab(frame, label_to_click)
            except Exception as exc:  # noqa: BLE001 - classified by the caller
                raise _ClickError(exc) from exc
            response = wait_for(pred, budget)
            if response is not None:
                return response
            _sleep(CLICK_RETRY_PAUSE)
        return None

    try:
        active = _probe_active_tab(frame, label)
        if active is True:
            # Nothing would be requested without leaving the tab first. The
            # detour's own answer is irrelevant (it carries another tag), so wait
            # for *any* list response to know the SPA refetched.
            click_and_wait(_detour_label(label), DETOUR_WAIT, pred=any_list_response)
            _sleep(TAB_SETTLE_PAUSE)
        response = click_and_wait(label, timeout)
        if response is None:
            on_event("点目标标签未拿到响应，改走「对面标签 → 目标标签」再试一次")
            click_and_wait(_detour_label(label), DETOUR_WAIT, pred=any_list_response)
            _sleep(TAB_SETTLE_PAUSE)
            response = click_and_wait(label, timeout)
        if response is None:
            # Never fall back to "some other tab's list": that is precisely how a
            # 待发起配送 order was silently dropped (2026-09-20). Fail loudly
            # instead — the message names the tag, so a Meituan rename of the tab
            # tag is diagnosable.
            raise TransientBridgeError(
                f"未捕获到 tag={wanted_tag} 的列表响应（{label} 标签），"
                "标签请求体可能已改版；本次未归档任何订单列表"
            )
    except _ClickError as err:
        exc = err.cause
        # A reload/navigation between readiness and the click detaches the frame
        # we captured; re-resolve the iframe once and retry before giving up.
        fresh = page.frame(name=ORDER_FRAME_NAME)
        if fresh is None or fresh is frame:
            raise TransientBridgeError(
                f"点击订单标签失败：{type(exc).__name__}: {exc}"
            ) from exc
        frame = fresh
        try:
            # Force a state change unconditionally: after an iframe swap we no
            # longer know which tab the SPA considers selected.
            click_and_wait(_detour_label(label), DETOUR_WAIT)
            response = click_and_wait(label, timeout)
            if response is None:
                raise TransientBridgeError(
                    "超时未捕获到订单列表接口响应（页面可能已卡死或未加载完）"
                )
        except _ClickError as retry_err:
            inner = retry_err.cause
            raise TransientBridgeError(
                f"点击订单标签失败：{type(inner).__name__}: {inner}"
            ) from inner
    finally:
        try:
            page.remove_listener("response", listener)
        except Exception:  # noqa: BLE001 - listener cleanup is best effort
            pass

    payload = _read_payload(response)
    if payload is None:
        raise TransientBridgeError("捕获到的响应无法解析为 JSON。")

    data = payload.get("data")
    if not isinstance(data, dict) or "orderList" not in data:
        # A credential failure is not a page problem: the SPA happily returns a
        # login-required envelope, and reloading it only wastes attempts.
        if payload.get("code") in AUTH_FAILURE_CODES:
            raise AuthExpiredError(
                f"接口返回需要登录（code={payload.get('code')}），"
                "刷新无法恢复，请在 Edge 中重新登录后再试。"
            )
        raise TransientBridgeError(
            f"接口响应未包含订单列表（code={payload.get('code')}）。"
        )
    return payload


def pull_order_list(
    root,
    cdp_url: str = "http://127.0.0.1:9222",
    timeout: int = 30,
    max_attempts: int = 3,
    reload_timeout: int = 30,
    ready_timeout: int = 20,
    on_event=None,
):
    """Connect to the local browser, capture the order-list responses and archive
    them under ``raw/`` plus ``data/latest-new-orders.json``.

    Two tabs are captured per pull (``TARGET_TABS``) because their order sets are
    disjoint — ``待接单`` (tag ``order_new``) and ``进行中`` (tag
    ``order_processing``, which carries 待发起配送 and every later live state) —
    and the responses are merged into one archived payload. Which statuses are
    worth pushing is still decided in :mod:`normalize`.

    The live ``mtgsig`` signature is produced by the browser itself, so no saved
    request template or auth file is needed — we only reuse the browser's
    logged-in session via CDP.

    ``max_attempts`` counts capture attempts, so ``3`` means "try, recover the
    page, try, recover, try". ``ready_timeout`` bounds how long one attempt
    waits for the page to become clickable — short enough that an unattended
    ``watch`` cycle stays near its interval even while the page is unusable.

    A failed pull writes a diagnostic snapshot to ``data/last-pull-error.json``
    and raises ``BridgeError`` (``AuthExpiredError`` when a human must log in
    again — that case is never retried).
    """
    from playwright.sync_api import sync_playwright

    def _emit(message: str) -> None:
        if on_event is not None:
            on_event(message)

    max_attempts = max(1, int(max_attempts))
    retries_used = 0
    recoveries = 0
    detail = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cdp_url": cdp_url,
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            detail.update(
                {
                    "stage": "connect",
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": "浏览器未启动或未开启远程调试端口",
                }
            )
            save_diagnostics(root, detail)
            raise BridgeError(
                f"无法连接浏览器 CDP（{cdp_url}）。请确认已按 README 启动带远程"
                f"调试的 Edge 并登录美团：{exc}"
            ) from exc

        try:
            page = _find_order_page(browser)
            if page is None:
                page = open_order_page(browser)
            if page is None:
                detail.update(
                    {
                        "stage": "find_page",
                        "error": "未找到美团订单页，且无法自动打开",
                    }
                )
                save_diagnostics(root, detail)
                raise BridgeError(
                    "未找到已登录的美团订单页，且无法自动打开。请在 Edge 中打开订单"
                    f"页面：{ORDER_PAGE_URL}"
                )

            # The order tabs live inside the `hashframe` iframe, not the top
            # document, so all clicks and response waits must target that frame.
            label = TARGET_TAB

            last_exc = None
            for attempt in range(max_attempts):
                # Check the session *before* waiting for the page to settle: on
                # the login page the order iframe never appears, so waiting
                # would burn the whole readiness timeout only to report a
                # timeout instead of the real (actionable) cause.
                if _login_check(page) == "login":
                    detail["attempts"] = attempt + 1
                    _raise_auth_expired(
                        root,
                        page,
                        detail,
                        _emit,
                        "页面位于登录页，未进入订单页",
                    )
                try:
                    # Wait (best effort) until the page is clickable again,
                    # then fetch the scope *now* rather than when the page was
                    # first found: a reload recreates the iframe, and clicking a
                    # frame captured before a refresh fails with
                    # "Frame was detached".
                    _wait_until_ready(
                        page,
                        None,
                        label,
                        timeout=ready_timeout,
                        page_probe=_login_check,
                    )
                    frame = page.frame(name=ORDER_FRAME_NAME) or page
                    payload = _capture_order_lists(page, frame, timeout, _emit)
                except AuthExpiredError as exc:
                    # Raised by _capture_order_list (e.g. the endpoint answered
                    # 401/403). No page recovery is attempted: only a human can
                    # log in again.
                    _raise_auth_expired(root, page, detail, _emit, str(exc))
                except TransientBridgeError as exc:
                    last_exc = exc
                    if not _should_recover(page):
                        detail["attempts"] = attempt + 1
                        _raise_auth_expired(
                            root,
                            page,
                            detail,
                            _emit,
                            str(exc) + "（页面已在登录页）",
                        )
                    if attempt + 1 >= max_attempts:
                        break
                    retries_used += 1
                    _emit(
                        f"第 {attempt + 1} 次尝试失败（{exc}），准备刷新页面后重试"
                    )
                    if not _reload_page(
                        page,
                        _recovery_tier(page, label, recoveries),
                        reload_timeout,
                        _emit,
                    ):
                        break
                    recoveries += 1
                    # Let the SPA settle before probing readiness again.
                    _sleep(2)
                    continue
                else:
                    # Archive the raw response first, unconditionally — a later
                    # summary failure must never cost us the original data. The
                    # summary is then generated best-effort (a malformed order is
                    # skipped, not fatal).
                    raw_path = save_raw(Path(root), payload)
                    summary = summarize_orders(payload)
                    summary_path = save_summary(Path(root), summary, kind="new")
                    if retries_used or recoveries:
                        _emit(
                            f"自愈成功：刷新 {recoveries} 次、重试 {retries_used} 次后"
                            "抓到订单列表"
                        )
                    return raw_path, summary_path

            detail.update(
                {
                    "stage": "capture",
                    "attempts": max_attempts,
                    "recoveries": recoveries,
                    "error": str(last_exc) if last_exc else "未知错误",
                    "page": _diagnose(page),
                }
            )
            path = save_diagnostics(root, detail)
            if path is not None:
                _emit(f"现场诊断：{path}")
            raise BridgeError(
                f"刷新重试 {max_attempts} 次后仍未捕获到订单列表：{last_exc}。"
                "若页面已掉登录，请在 Edge 中重新登录；若整个浏览器无响应，"
                "需强制退出并重新启动带远程调试的 Edge。"
            )
        # 注意：connect_over_cdp 连接的是用户自己的浏览器，绝不能调用
        # browser.close()，否则会关闭用户正在使用的 Edge。依赖
        # sync_playwright 上下文退出时自动断开连接即可，不主动关闭。
        finally:
            pass
