import unittest

from src.mt_monitor.normalize import summarize_orders


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
