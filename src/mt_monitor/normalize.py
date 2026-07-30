import json


def summarize_orders(payload):
    summaries = []
    for order in payload.get("data", {}).get("orderList", []):
        common = json.loads(order["commonInfo"])
        info = json.loads(order["orderInfo"])
        basic = info["unifiedBasicInfo"]
        items = [
            {"name": detail["foodName"], "quantity": detail["count"]}
            for cart in info["foodInfo"].get("cartDetails", [])
            for detail in cart.get("details", [])
        ]
        summaries.append({
            "order_id": str(common["wm_order_id_view"]),
            "status": basic["orderStatusDesc"],
            "store": basic["wmPoiName"],
            "user_paid": info["chargeInfo"]["userPayTotalAmount"],
            "items": items,
        })
    return summaries
