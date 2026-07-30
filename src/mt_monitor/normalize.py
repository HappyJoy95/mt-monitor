import json
import sys


def _summarize_one(order):
    """Extract a single order summary from a raw order dict.

    Raises on malformed input (missing field, broken nested JSON, unexpected
    shape) so the caller can decide how to handle that one order.
    """
    common = json.loads(order["commonInfo"])
    info = json.loads(order["orderInfo"])
    basic = info["unifiedBasicInfo"]
    items = [
        {"name": detail["foodName"], "quantity": detail["count"]}
        for cart in info["foodInfo"].get("cartDetails", [])
        for detail in cart.get("details", [])
    ]
    return {
        "order_id": str(common["wm_order_id_view"]),
        "status": basic["orderStatusDesc"],
        "store": basic["wmPoiName"],
        "user_paid": info["chargeInfo"]["userPayTotalAmount"],
        "items": items,
    }


def summarize_orders(payload):
    """Best-effort summary of every order in ``payload``.

    A single malformed order (missing field, unexpected shape, broken nested
    JSON) is skipped with a warning printed to stderr — it never aborts the
    whole collection. The raw response is archived separately, so even if every
    order fails to summarize, the original data is still preserved on disk.
    """
    summaries = []
    order_list = (payload.get("data") or {}).get("orderList") or []
    for index, order in enumerate(order_list):
        try:
            summaries.append(_summarize_one(order))
        except Exception as exc:
            order_id = "?"
            try:
                order_id = json.loads(order.get("commonInfo", "{}")).get(
                    "wm_order_id_view", "?"
                )
            except Exception:
                pass
            print(
                f"⚠️ 跳过第 {index} 笔订单（order_id={order_id}）摘要失败：{exc}",
                file=sys.stderr,
            )
    return summaries
