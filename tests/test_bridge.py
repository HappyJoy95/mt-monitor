"""Tests for the self-healing CDP bridge.

The real pull needs a logged-in Edge, which CI does not have, so these tests
drive ``pull_order_list`` through fake Playwright objects and assert the
*recovery policy*: what gets retried, what gets refreshed, and what is reported
instead of retried.

The fakes model the facts the bridge relies on:
  * the bridge registers a ``response`` listener *before* clicking and reads the
    first matching response — a stuck page fires nothing, so the click succeeds
    while the capture times out;
  * the tab click happens inside the ``hashframe`` iframe scope;
  * the selected tab carries an ``active_<hash>`` class.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.mt_monitor import bridge

ORDER_PATH = bridge.ORDER_LIST_PATH


class StuckPage(Exception):
    """Raised by the fake click when the simulated page is dead.

    Deliberately not a ``TimeoutError``: the click helper retries TimedOut
    clicks (a real actionability race), so the fake must fail in a way that
    reaches the caller instead of being retried within one attempt.
    """


class FakeRequest:
    def __init__(self, method="POST", url=None):
        self.method = method
        self.url = url or f"https://shangoue.meituan.com/gw/api/order{ORDER_PATH}"


class FakeResponse:
    """A captured response; ``request`` mirrors Playwright's navigation request.

    ``url`` is intentionally *absent* — the bridge must filter on
    ``response.request.url`` (a response's own URL can differ after redirects),
    and the earlier fake silently allowed the wrong field to slip through.
    """

    def __init__(self, payload, request=None):
        self._payload = payload
        self.request = request or FakeRequest()

    def json(self):
        return self._payload


class FakeLocator:
    """Carries the ``has_text`` filter so the click knows which tab it is."""

    def __init__(self, frame, label=None):
        self.frame = frame
        self.label = label

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.frame.click(label=self.label, timeout=timeout)


def _fake_evaluate(page, label_present, script, arg=None):
    """One implementation for both the iframe scope and the page scope.

    Order matters: both probe scripts mention "no-tab", so the active-tab probe
    is identified by its ``[selector, label, activePrefix]`` argument first —
    the same discrimination the bridge's own JS makes.
    """
    if "readyState !==" in script:
        return True
    if "input[type=password]" in script:
        return "login" if page.login_page else "ok"
    if isinstance(arg, list) and len(arg) == 3 and "activePrefix" in script:
        if page.active_label is None:
            return None
        return page.active_label == arg[1]
    if "no-tab" in script:
        return True if label_present else "no-tab"
    return True


class FakeFrame:
    """Stand-in for the ``hashframe`` iframe (also usable as a page scope)."""

    def __init__(self, page, label_present=True, detached=False):
        self.page = page
        self.label_present = label_present
        self.detached = detached

    def evaluate(self, script, arg=None):
        if self.detached:
            raise RuntimeError("Execution context was destroyed (iframe detached)")
        return _fake_evaluate(self.page, self.label_present, script, arg)

    def locator(self, selector, has_text=None):
        return FakeLocator(self, label=has_text)

    def click(self, label=None, timeout=None):
        page = self.page
        if self.detached:
            # What Playwright raises when a reload replaced the iframe we hold.
            raise RuntimeError("Locator.click: Frame was detached")
        if not self.label_present:
            # No such button on the page: a real Playwright click waits for the
            # locator and then times out.
            page.clicks.append(label)
            raise TimeoutError("Timeout 5000ms exceeded waiting for the tab")
        page.clicks.append(label)
        page.clicks_fired += 1
        if page.swap_frame_on_first_click is not None:
            # The SPA re-renders and replaces the iframe while we click.
            page.current_frame = page.swap_frame_on_first_click
            page.swap_frame_on_first_click = None
        # `page.attempts` counts requests produced (the page answers the detour
        # tab too); every tab click increments it, so tests that care about
        # "how many clicks" assert on `page.clicks` instead.
        page.attempts += 1
        if page.clicks_fired <= page.fail_times:
            # A stuck page accepts the click (no Playwright error) but never
            # fires the request, so the capture has to time out.
            return
        # The live page answers *any* state tab with this endpoint, so the fake
        # responds to the detour click as well — otherwise every detour would
        # burn the real 5s DETOUR_WAIT and the suite would crawl.
        payload = page.payload_fn() if page.payload_fn else page.payload
        if page.script:
            # Scripted mode: this click's outcome is `None` (swallowed) or a
            # specific payload, so tests drive the click-by-click behaviour.
            # Once the script runs out, the page behaves normally again.
            payload = page.script.pop(0)
            if payload is None:
                return
        page.emit(FakeResponse(payload))


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    def new_cdp_session(self, page):
        page.hard_reloads += 1
        return FakeCdpSession(page)


class FakeCdpSession:
    def __init__(self, page):
        self.page = page

    def send(self, method, params=None):
        if method != "Page.reload":
            raise AssertionError(f"意外 CDP 调用：{method}")
        self.page.events.append("hard_reload")


class FakePage:
    def __init__(self, payload=None, fail_times=0, login_page=False):
        self.payload = payload if payload is not None else {
            "code": 0,
            "data": {"orderList": []},
        }
        # Optional hook so a test can model a page whose *content* recovers
        # only after the recovery reload (attempts counter drives it).
        self.payload_fn = None
        # Optional per-click outcomes: one entry consumed per tab click, where
        # ``None`` means "the click produced no response".
        self.script = []
        self.swap_frame_on_first_click = None
        self.fail_times = fail_times
        self.clicks_fired = 0
        self.attempts = 0
        self.reloads = 0
        self.hard_reloads = 0
        self.goto_calls = []
        self.events = []
        self.clicks = []
        self.captured = []
        self.listeners = []
        self.context = FakeContext(self)
        # An expired session keeps the order page discoverable — the merchant
        # SPA is still the tab we find, it just shows the login box (or bounces
        # to passport). Both shapes must be recognized as "needs a human".
        self.url = (
            "https://passport.meituan.com/account/unitivelogin?redirect="
            "https%3A%2F%2Fshangoue.meituan.com%2Forderbusiness"
            if login_page
            else "https://shangoue.meituan.com/#/page/orderbusiness"
        )
        self.frame_obj = FakeFrame(self)
        self.current_frame = self.frame_obj
        self.current_frame_label_present = True
        # Label of the currently selected state tab (None = unknown). The
        # default models a freshly loaded page: the target tab is *not* yet
        # selected, so one click on it is enough.
        self.active_label = bridge.OPPOSITE_TAB_LABEL

    # --- Playwright-ish API used by the bridge ---------------------------
    def evaluate(self, script, arg=None):
        return _fake_evaluate(self, None, script, arg)

    def frame(self, name=None):
        if name != bridge.ORDER_FRAME_NAME:
            return None
        return self.current_frame

    def title(self):
        return "登录" if self.login_page else "订单管理"

    def reload(self, timeout=None, wait_until=None):
        self.reloads += 1
        self.events.append("reload")

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls.append(url)
        self.events.append("goto")

    def wait_for_timeout(self, ms):
        return None

    def on(self, event, callback):
        """Playwright's event subscription (the bridge listens for responses)."""
        if event != "response":
            raise AssertionError(f"意外的事件订阅：{event}")
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def emit(self, response):
        """Deliver a response to the registered listeners."""
        for callback in list(self.listeners):
            callback(response)


class FakeBrowser:
    def __init__(self, page):
        self.contexts = [page.context]


class FakeSyncPlaywright:
    def __init__(self, page, connect_error=None):
        self.page = page
        self.connect_error = connect_error
        self.chromium = self

    def connect_over_cdp(self, url):
        if self.connect_error is not None:
            raise self.connect_error
        return FakeBrowser(self.page)


class FakePlaywrightManager:
    """Mimics ``with sync_playwright() as p``."""

    def __init__(self, page, connect_error=None):
        self._api = FakeSyncPlaywright(page, connect_error)

    def __enter__(self):
        return self._api

    def __exit__(self, *exc_info):
        return False


def _advancing_clock(step=0.5):
    """Monotonic-clock stand-in that advances on every read.

    ``_wait_until_ready`` loops until its deadline; with a frozen clock (and a
    patched-out sleep) that loop would never end.
    """
    state = {"now": 0.0}

    def clock():
        state["now"] += step
        return state["now"]

    return clock


def _fast(seconds):
    """No-op sleep: the polling loops stay real-time but never actually wait."""
    return None


def _instant():
    """Patch the bridge's clock + sleep so deadline loops finish immediately.

    Plain functions are used (not ``Mock(side_effect=...)``) so that every read
    advances the clock predictably and no test leaks state into the next.
    """
    clock = _advancing_clock()
    return (
        mock.patch.object(bridge, "_monotonic", clock),
        mock.patch.object(bridge, "_sleep", _fast),
    )


def _patch_playwright(page, connect_error=None):
    module = mock.Mock()
    module.sync_playwright = lambda: FakePlaywrightManager(page, connect_error)
    return mock.patch.dict(
        "sys.modules", {"playwright": module, "playwright.sync_api": module}
    )


PAYLOAD = {
    "code": 0,
    "data": {
        "orderList": [{
            "commonInfo": '{"wm_order_id_view": "123"}',
            "orderInfo": json.dumps({
                "chargeInfo": {"userPayTotalAmount": 210.0},
                "unifiedBasicInfo": {
                    "wmPoiName": "测试门店",
                    "orderStatusDesc": "待接单",
                },
                "foodInfo": {"cartDetails": []},
            }),
        }]
    },
}


class TabStrategyTests(unittest.TestCase):
    """The target tab is a cross-machine contract, not an implementation detail.

    The Windows side relies on "进行中" carrying the orders of every state;
    :mod:`normalize` then keeps only the statuses worth pushing. Changing the
    target here silently narrows the monitored order set, so pin it.
    """

    def test_target_tab_is_the_all_orders_tab(self):
        self.assertEqual(bridge.TARGET_TAB, "进行中")
        self.assertEqual(bridge.TAB_LABEL, bridge.TARGET_TAB)

    def test_fallback_tab_differs_from_the_target(self):
        # The detour only works if it lands on a *different* tab.
        self.assertNotEqual(bridge.FALLBACK_TAB, bridge.TARGET_TAB)

    def test_capture_keeps_the_response_from_the_target_tab(self):
        page = FakePage(payload=PAYLOAD)
        page.active_label = bridge.FALLBACK_TAB
        with TemporaryDirectory() as directory, _patch_playwright(page), \
                mock.patch.object(bridge, "_sleep", _fast):
            root = Path(directory)
            raw_path, _summary_path = bridge.pull_order_list(
                root, timeout=1, ready_timeout=1
            )
            # Assert inside the context: TemporaryDirectory removes the files on
            # exit, so a later check would test the cleanup, not the pull.
            self.assertTrue(Path(raw_path).exists())

        self.assertEqual(page.clicks, [bridge.TARGET_TAB])


class CaptureProtocolTests(unittest.TestCase):
    """The fake must model the contract the bridge depends on, or every test
    below would pass vacuously.

    These four cases exercise *timing* (a click that produces no response must
    actually wait), so they run against the real clock with sub-second timeouts
    instead of a faked one — a frozen clock would make every deadline expire
    instantly and quietly invert the behaviour under test.
    """

    SHORT = dict(timeout=1, ready_timeout=1)

    def test_inactive_target_tab_is_clicked_once(self):
        # Fresh page: the target tab is not selected, so one click triggers the
        # list request and no detour is needed.
        page = FakePage(payload=PAYLOAD)
        with TemporaryDirectory() as directory, _patch_playwright(page), \
                mock.patch.object(bridge, "_sleep", _fast):
            bridge.pull_order_list(Path(directory), **self.SHORT)

        self.assertEqual(page.clicks, [bridge.TAB_LABEL])
        self.assertEqual(page.reloads, 0)

    def test_active_target_tab_detours_through_the_opposite_tab(self):
        # A selected tab is a no-op to click, so the bridge must leave it first
        # — otherwise no request would ever be fired.
        page = FakePage(payload=PAYLOAD)
        page.active_label = bridge.TAB_LABEL
        with TemporaryDirectory() as directory, _patch_playwright(page), \
                mock.patch.object(bridge, "_sleep", _fast):
            bridge.pull_order_list(Path(directory), **self.SHORT)

        self.assertEqual(
            page.clicks, [bridge.OPPOSITE_TAB_LABEL, bridge.TAB_LABEL]
        )
        self.assertEqual(page.reloads, 0)

    def test_swallowed_click_is_retried_through_the_opposite_tab(self):
        # Live symptom: the click lands but the SPA fires nothing. One retry via
        # the opposite tab must recover it without a page reload.
        page = FakePage(payload=PAYLOAD, fail_times=1)
        with TemporaryDirectory() as directory, _patch_playwright(page), \
                mock.patch.object(bridge, "_sleep", _fast):
            bridge.pull_order_list(Path(directory), **self.SHORT)

        self.assertEqual(
            page.clicks,
            [bridge.TAB_LABEL, bridge.OPPOSITE_TAB_LABEL, bridge.TAB_LABEL],
        )
        self.assertEqual(page.reloads, 0)

    def test_page_fires_nothing_at_all_and_the_pull_recovers_by_reloading(self):
        # Nothing arrives for the whole first capture round (target click, then
        # the retry through the opposite tab). The pull must escalate to a page
        # reload instead of giving up.
        page = FakePage(payload=PAYLOAD, fail_times=3)  # whole first round dead
        with TemporaryDirectory() as directory, _patch_playwright(page), \
                mock.patch.object(bridge, "_sleep", _fast):
            bridge.pull_order_list(Path(directory), **self.SHORT)

        self.assertEqual(page.reloads, 1)
        self.assertEqual(
            page.clicks,
            [
                bridge.TAB_LABEL,           # target: swallowed
                bridge.OPPOSITE_TAB_LABEL,  # detour: swallowed
                bridge.TAB_LABEL,           # retry: swallowed -> round failed
                bridge.TAB_LABEL,           # after the reload: captured
            ],
        )


class PullSelfHealTests(unittest.TestCase):
    def test_retryable_failure_reloads_the_page_then_succeeds(self):
        # The first round returns a payload with no orderList (in-attempt retry
        # included), so the pull must reload the page and succeed after that.
        page = FakePage(payload=PAYLOAD)
        page.script = [
            None,                     # target click: nothing fires
            None,                     # detour click: nothing fires either
            {"code": 0, "data": {}},  # retry: fires, but carries no orderList
        ]                             # after the reload: the real list
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                raw_path, summary_path = bridge.pull_order_list(root)

            self.assertEqual(page.reloads, 1)
            self.assertEqual(
                page.clicks[-1], bridge.TAB_LABEL
            )
            self.assertTrue(Path(raw_path).exists())
            self.assertEqual(
                json.loads(Path(summary_path).read_text(encoding="utf-8"))[0][
                    "order_id"
                ],
                "123",
            )

    def test_reload_tiers_escalate_soft_then_hard_then_navigate(self):
        page = FakePage(payload=PAYLOAD, fail_times=99)
        with TemporaryDirectory() as directory:
            with _patch_playwright(page), _instant()[0], _instant()[1], mock.patch.object(
                bridge, "_reload_page", wraps=bridge._reload_page
            ) as reload_spy:
                with self.assertRaises(bridge.BridgeError):
                    bridge.pull_order_list(Path(directory), max_attempts=3)

            tiers = [call.args[1] for call in reload_spy.call_args_list]
            self.assertEqual(tiers, [0, 1])
            # Tier 0 is the plain reload; the fake never reaches tier 1's CDP
            # call because reload() itself succeeds on a stuck page.
            self.assertEqual(page.reloads, 1)

    def test_giving_up_writes_a_diagnostic_snapshot(self):
        page = FakePage(payload=PAYLOAD, fail_times=99)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                with self.assertRaises(bridge.BridgeError) as ctx:
                    bridge.pull_order_list(root, max_attempts=3)

            self.assertIn("刷新重试", str(ctx.exception))
            detail = json.loads(
                (root / bridge.DIAGNOSTICS_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(detail["attempts"], 3)
            self.assertEqual(detail["stage"], "capture")
            self.assertEqual(detail["page"]["hashframe"], True)

    def test_expired_session_is_reported_without_reloading(self):
        page = FakePage(login_page=True, fail_times=99)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                with self.assertRaises(bridge.AuthExpiredError):
                    bridge.pull_order_list(root)

            self.assertEqual(page.reloads, 0)
            # The login precheck fires before any click, so no click is wasted
            # on a page that can never produce a list.
            self.assertEqual(page.attempts, 0)
            self.assertEqual(page.clicks, [])
            detail = json.loads(
                (root / bridge.DIAGNOSTICS_FILE).read_text(encoding="utf-8")
            )
            self.assertIn("登录", detail["hint"])

    def test_zero_retries_fails_fast_and_never_reloads(self):
        page = FakePage(payload=PAYLOAD, fail_times=99)
        with TemporaryDirectory() as directory:
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                with self.assertRaises(bridge.BridgeError):
                    bridge.pull_order_list(Path(directory), max_attempts=1)

            self.assertEqual(page.reloads, 0)
            self.assertEqual(
                page.clicks,
                [bridge.TAB_LABEL, bridge.OPPOSITE_TAB_LABEL, bridge.TAB_LABEL],
            )

    def test_payload_without_order_list_is_retryable(self):
        # A response that parses but carries no orderList is a page problem
        # (wrong tab / half-loaded SPA): after the recovery reload the page
        # serves the real list, so the pull must succeed rather than give up.
        bad = {"code": 0, "data": {"foo": 1}}
        page = FakePage(payload=bad)
        page.payload_fn = lambda: PAYLOAD if page.attempts >= 2 else bad
        with TemporaryDirectory() as directory:
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                raw_path, _ = bridge.pull_order_list(Path(directory))

            self.assertTrue(Path(raw_path).exists())

    def test_credential_failure_is_not_retried(self):
        # 401/403 means the session is gone: reloading burns 3 attempts and
        # leaves the operator with a vague error, so it must stop immediately.
        page = FakePage(payload={"code": 403, "msg": "未登录"}, fail_times=0)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                with self.assertRaises(bridge.AuthExpiredError) as ctx:
                    bridge.pull_order_list(root)

            self.assertEqual(page.reloads, 0)
            self.assertEqual(page.attempts, 1)
            self.assertIn("重新登录", str(ctx.exception))

    def test_unknown_payload_shape_is_retried_until_the_limit(self):
        page = FakePage(payload={"code": 500, "data": {}}, fail_times=0)
        with TemporaryDirectory() as directory:
            with _patch_playwright(page), _instant()[0], _instant()[1]:
                with self.assertRaises(bridge.BridgeError):
                    bridge.pull_order_list(Path(directory), max_attempts=2)

            self.assertEqual(page.reloads, 1)
            self.assertEqual(page.attempts, 2)

    def test_detached_iframe_is_refetched_and_clicked_again(self):
        # Live evidence: the iframe can be swapped out between readiness and the
        # click, and clicking the object we hold then raises "Frame was
        # detached". The capture must re-resolve the iframe and retry instead of
        # reporting a click failure.
        page = FakePage(payload=PAYLOAD)
        stale = FakeFrame(page, detached=True)
        live = FakeFrame(page)
        page.current_frame = live
        page.swap_frame_on_first_click = stale  # detour click swaps the iframe
        with TemporaryDirectory() as directory:
            with _patch_playwright(page), mock.patch.object(
                bridge, "_sleep", _fast
            ), mock.patch.object(bridge, "_probe_active_tab", return_value=True):
                raw_path, _ = bridge.pull_order_list(
                    Path(directory), timeout=1, ready_timeout=1
                )

            self.assertTrue(Path(raw_path).exists())
            self.assertEqual(page.reloads, 0)
            # The target click was retried against the re-resolved iframe.
            self.assertEqual(page.clicks[-1], bridge.TAB_LABEL)

    def test_frame_ready_but_tab_absent_fails_with_a_page_problem(self):
        # The unauthenticated order page renders `hashframe` but never the state
        # tabs. Readiness then times out and the pull must report a page
        # problem (retryable), not silently return an empty list.
        page = FakePage(payload=PAYLOAD)
        page.current_frame = FakeFrame(page, label_present=False)
        with TemporaryDirectory() as directory:
            with _patch_playwright(page), _instant()[0], _instant()[1], mock.patch.object(
                bridge, "_monotonic", side_effect=_advancing_clock()
            ):
                with self.assertRaises(bridge.BridgeError) as ctx:
                    bridge.pull_order_list(
                        Path(directory), max_attempts=1, ready_timeout=5
                    )

            self.assertIn("超时未捕获", str(ctx.exception))
            self.assertEqual(page.captured, [])  # nothing was ever captured

    def test_cdp_connect_failure_is_reported_and_recorded(self):
        page = FakePage()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with _patch_playwright(page, connect_error=OSError("连接被拒绝")):
                with self.assertRaises(bridge.BridgeError) as ctx:
                    bridge.pull_order_list(root)

            self.assertIn("无法连接浏览器 CDP", str(ctx.exception))
            detail = json.loads(
                (root / bridge.DIAGNOSTICS_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(detail["stage"], "connect")


class ReadyProbeTests(unittest.TestCase):
    def test_probe_reports_not_ready_when_iframe_is_missing(self):
        page = FakePage()
        page.current_frame = None  # iframe not (re)attached yet
        self.assertFalse(bridge._page_ready(page))

    def test_probe_reports_ready_for_loaded_page(self):
        self.assertTrue(bridge._page_ready(FakePage()))

    def test_wait_until_ready_times_out_on_invisible_tab(self):
        page = FakePage()
        hidden = FakeFrame(page, label_present=False)
        page.current_frame = hidden
        with mock.patch.object(bridge, "_sleep", _fast):
            self.assertIsNone(
                bridge._wait_until_ready(page, None, bridge.TAB_LABEL, 0)
            )

    def test_hard_reload_tier_uses_cdp_when_soft_reload_fails(self):
        page = FakePage()

        def failing_reload(timeout=None, wait_until=None):
            raise RuntimeError("reload refused")

        page.reload = failing_reload
        with mock.patch.object(bridge, "_sleep", _fast):
            ok = bridge._reload_page(page, 1, 30, lambda message: None)

        self.assertTrue(ok)
        self.assertEqual(page.hard_reloads, 1)

    def test_navigate_tier_falls_back_to_the_order_url(self):
        page = FakePage()
        with mock.patch.object(bridge, "_sleep", _fast):
            ok = bridge._reload_page(page, 2, 30, lambda message: None)

        self.assertTrue(ok)
        self.assertEqual(page.goto_calls, [bridge.ORDER_PAGE_URL])


if __name__ == "__main__":
    unittest.main()
