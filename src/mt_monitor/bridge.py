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
    and the page requests it **only when its tab ("待接单" / "进行中") is
    (re)selected**. So we trigger the list by clicking the target tab rather
    than reloading.
"""
from __future__ import annotations

from pathlib import Path

from .normalize import summarize_orders
from .storage import save_raw, save_summary

ORDER_LIST_PATH = "/order/list/page/unprocessed"
ORDER_PAGE_MARKERS = ("shangoue.meituan.com", "orderbusiness")
# Only the "待接单" (pending) tab is monitored; the opposite tab is clicked
# first purely to guarantee a fresh list request (see pull_order_list).
TAB_LABEL = "待接单"
OPPOSITE_TAB_LABEL = "进行中"


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


def _click_tab(scope, label) -> None:
    """Click the order-state tab with the given label to trigger a fresh list
    request. The merchant SPA fetches the order list only when its tab is
    (re)selected, not on a plain reload.

    ``scope`` is the page or (more reliably) the ``hashframe`` iframe, because
    the order tabs live inside that iframe rather than the top document. The tab
    buttons carry the stable class ``tab-btn_c17`` and a text of the form
    "<label> <count>" (e.g. "待接单 0"), so we match by class + text prefix and
    use a real Playwright click (a synthetic ``el.click()`` can miss the SPA's
    event handler).
    """
    try:
        scope.locator("button.tab-btn_c17", has_text=label).first.click(
            timeout=5000
        )
        return
    except Exception:
        pass
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
    except Exception:
        pass


def pull_order_list(
    root,
    cdp_url: str = "http://127.0.0.1:9222",
    timeout: int = 30,
):
    """Connect to the local browser, capture the "待接单" (pending) order-list
    response and archive it under ``raw/`` plus ``data/latest-new-orders.json``.

    The live ``mtgsig`` signature is produced by the browser itself, so no saved
    request template or auth file is needed — we only reuse the browser's
    logged-in session via CDP.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接浏览器 CDP（{cdp_url}）。请确认已按 README 启动带远程"
                f"调试的 Edge 并登录美团：{exc}"
            )

        try:
            page = _find_order_page(browser)
            if page is None:
                raise RuntimeError(
                    "未找到已登录的美团订单页。请在 Edge 中打开订单页面："
                    "https://shangoue.meituan.com/#/page/orderbusiness"
                    "#/order/unprocessed"
                )

            # The order tabs live inside the `hashframe` iframe, not the top
            # document, so all clicks and response waits must target that frame.
            frame = page.frame(name="hashframe") or page

            label = TAB_LABEL
            opposite_label = OPPOSITE_TAB_LABEL
            # Match the list endpoint exactly; the count/interval endpoints also
            # live under /order/list/ but carry no orderList.
            pred = (
                lambda r: getattr(r.request, "method", "") == "POST"
                and ORDER_LIST_PATH in (r.url or "")
            )
            # The SPA only refetches the order list when its tab is (re)selected,
            # and re-clicking an *already active* tab is a no-op (no request).
            # To guarantee a fresh request for `label`, first switch to the
            # opposite tab — its response is captured and discarded — then switch
            # back to `label`, whose response we keep. If the page is already on
            # the opposite tab, the first click is a no-op and times out, which
            # is harmless.
            try:
                with page.expect_response(pred, timeout=timeout * 1000):
                    _click_tab(frame, opposite_label)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            try:
                with page.expect_response(pred, timeout=timeout * 1000) as info:
                    _click_tab(frame, label)
                response = info.value
            except Exception as exc:
                if "Timeout" in type(exc).__name__:
                    raise RuntimeError(
                        "超时未捕获到订单列表接口响应。若页面已掉登录或列表未加载，"
                        "请在 Edge 中刷新/重新登录后重试。"
                    )
                raise

            payload = _read_payload(response)
            if payload is None:
                raise RuntimeError("捕获到的响应无法解析为 JSON，请重试。")

            data = payload.get("data")
            if not isinstance(data, dict) or "orderList" not in data:
                raise RuntimeError(
                    f"接口响应未包含订单列表（code={payload.get('code')}，"
                    "可能已掉登录或返回错误页）。请在 Edge 中刷新/重新登录后重试。"
                )

            # Archive the raw response first, unconditionally — a later summary
            # failure must never cost us the original data. The summary is then
            # generated best-effort (a malformed order is skipped, not fatal).
            raw_path = save_raw(Path(root), payload)
            summary = summarize_orders(payload)
            summary_path = save_summary(Path(root), summary, kind="new")
            return raw_path, summary_path
        # 注意：connect_over_cdp 连接的是用户自己的浏览器，绝不能调用
        # browser.close()，否则会关闭用户正在使用的 Edge。依赖
        # sync_playwright 上下文退出时自动断开连接即可，不主动关闭。
        finally:
            pass
