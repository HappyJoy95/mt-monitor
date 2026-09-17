import json
import unittest
from datetime import datetime

from src.mt_monitor.normalize import (
    PICK_READY_OFFSET_MINUTES,
    summarize_orders,
)


def _order(status, click_time=None, order_id="123"):
    """Build one raw order dict the way the merchant API delivers it.

    ``click_time`` (epoch seconds) becomes the ``pickUpButton``'s
    ``canClickButtonTime``; omit it to model an order whose button carries no
    timestamp at all.
    """
    buttons = []
    if click_time is not None:
        buttons.append({"btnDetailCode": "pickUpButton", "params": {"canClickButtonTime": click_time}})
    order_info = {
        "chargeInfo": {"userPayTotalAmount": 210.0},
        "unifiedBasicInfo": {"wmPoiName": "测试门店", "orderStatusDesc": status},
        "foodInfo": {"cartDetails": [{"details": [{"foodName": "测试商品", "count": 1}]}]},
        "merchantWorkflowInfo": {"orderOperateBtnList": buttons},
    }
    return {
        "commonInfo": json.dumps({"wm_order_id_view": order_id}, ensure_ascii=False),
        "orderInfo": json.dumps(order_info, ensure_ascii=False),
    }


def _summarize(*orders):
    return summarize_orders({"data": {"orderList": list(orders)}})


class SummarizeOrdersTests(unittest.TestCase):
    def test_summarize_orders_reads_nested_order_strings(self):
        payload = {
            "data": {"orderList": [{
                "commonInfo": '{"wm_order_id_view": "123", "orderStatus": 2}',
                "orderInfo": '{"chargeInfo": {"userPayTotalAmount": 210.0}, "unifiedBasicInfo": {"wmPoiName": "测试门店", "orderStatusDesc": "待接单"}, "foodInfo": {"cartDetails": [{"details": [{"foodName": "测试商品", "count": 1}]}]}}',
            }]}
        }

        self.assertEqual(summarize_orders(payload), [{
            "order_id": "123",
            "status": "待接单",
            "store": "测试门店",
            "user_paid": 210.0,
            "items": [{"name": "测试商品", "quantity": 1}],
        }])

    def test_summarize_skips_malformed_orders(self):
        good = {
            "commonInfo": '{"wm_order_id_view": "123", "orderStatus": 2}',
            "orderInfo": '{"chargeInfo": {"userPayTotalAmount": 210.0}, "unifiedBasicInfo": {"wmPoiName": "测试门店", "orderStatusDesc": "待接单"}, "foodInfo": {"cartDetails": []}}',
        }
        payload = {
            "data": {"orderList": [
                good,
                {"commonInfo": "not-json"},          # 嵌套 JSON 损坏
                {"orderInfo": "{}"},                  # 缺 commonInfo
            ]}
        }
        result = summarize_orders(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["order_id"], "123")


class DeliveryStatusGateTests(unittest.TestCase):
    """The 待发起配送 push window: ``canClickButtonTime - N`` minutes."""

    def test_offset_is_pinned_to_the_agreed_six_minutes(self):
        # Business decision (2026-09-17): give stores more lead time than the
        # original 3 minutes so they can prepare before 拣货完成 becomes clickable.
        self.assertEqual(PICK_READY_OFFSET_MINUTES, 6)

    def test_delivery_order_inside_the_window_is_pushed(self):
        now = datetime.now().timestamp()
        click_time = int(now + (PICK_READY_OFFSET_MINUTES - 1) * 60)

        result = _summarize(_order("待发起配送", click_time))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "待发起配送")
        self.assertEqual(result[0]["store"], "测试门店")

    def test_delivery_order_too_early_is_held_back(self):
        now = datetime.now().timestamp()
        # 拣货完成 is still further away than the window allows.
        click_time = int(now + (PICK_READY_OFFSET_MINUTES + 5) * 60)

        self.assertEqual(_summarize(_order("待发起配送", click_time)), [])

    def test_boundary_minute_is_pushed(self):
        # now >= threshold is inclusive: at exactly canClickButtonTime - N it pushes.
        now = datetime.now().timestamp()
        click_time = int(now + PICK_READY_OFFSET_MINUTES * 60)

        self.assertEqual(len(_summarize(_order("待发起配送", click_time))), 1)

    def test_delivery_without_click_time_is_pushed_immediately(self):
        # No pickUpButton timestamp to gate on → never hide the order.
        self.assertEqual(len(_summarize(_order("待发起配送"))), 1)

    def test_delivery_with_unparsable_click_time_is_pushed_immediately(self):
        order = _order("待发起配送")
        info = json.loads(order["orderInfo"])
        info["merchantWorkflowInfo"]["orderOperateBtnList"] = [
            {"btnDetailCode": "pickUpButton", "params": {"canClickButtonTime": "not-a-number"}}
        ]
        order["orderInfo"] = json.dumps(info, ensure_ascii=False)

        self.assertEqual(len(_summarize(order)), 1)

    def test_pending_acceptance_has_no_window(self):
        # 待接单 is pushed no matter what the workflow buttons say.
        self.assertEqual(len(_summarize(_order("待接单"))), 1)

    def test_other_statuses_are_skipped(self):
        for status in ("已完成", "已取消", "配送中"):
            with self.subTest(status=status):
                self.assertEqual(_summarize(_order(status)), [])

    def test_mixed_batch_keeps_only_pushable_orders(self):
        now = datetime.now().timestamp()
        result = _summarize(
            _order("待接单", order_id="A"),
            _order("待发起配送", int(now + (PICK_READY_OFFSET_MINUTES - 1) * 60), order_id="B"),
            _order("待发起配送", int(now + (PICK_READY_OFFSET_MINUTES + 10) * 60), order_id="C"),
            _order("已完成", order_id="D"),
        )
        self.assertEqual([o["order_id"] for o in result], ["A", "B"])
