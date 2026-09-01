import json
import sys
from datetime import datetime, timedelta

# Only these order statuses should be summarized and pushed
VALID_STATUSES = {"待接单", "待发起配送"}

# For "待发起配送" orders, only push when current time >= canClickButtonTime - 3 minutes
PICK_READY_OFFSET_MINUTES = 3


def _get_pick_ready_time(order):
    """Extract the canClickButtonTime for "拣货完成" button from order data.

    Returns the timestamp (int) or None if not found.
    """
    try:
        info = json.loads(order["orderInfo"])
        workflow = info.get("merchantWorkflowInfo", {})
        btn_list = workflow.get("orderOperateBtnList", [])
        for btn in btn_list:
            if btn.get("btnDetailCode") == "pickUpButton":
                params = btn.get("params", {})
                can_click_time = params.get("canClickButtonTime")
                if can_click_time:
                    return int(can_click_time)
    except Exception:
        pass
    return None


def _summarize_one(order):
    """Extract a single order summary from a raw order dict.

    Raises on malformed input (missing field, broken nested JSON, unexpected
    shape) so the caller can decide how to handle that one order.
    """
    common = json.loads(order["commonInfo"])
    info = json.loads(order["orderInfo"])
    basic = info["unifiedBasicInfo"]
    status = basic["orderStatusDesc"]

    # Skip orders not in valid statuses
    if status not in VALID_STATUSES:
        return None

    # For "待发起配送" orders, check if current time >= pickReadyTime - 3min
    if status == "待发起配送":
        pick_ready_time = _get_pick_ready_time(order)
        if pick_ready_time:
            threshold = pick_ready_time - (PICK_READY_OFFSET_MINUTES * 60)
            now = datetime.now().timestamp()
            if now < threshold:
                # Too early, don't push yet
                return None

    items = [
        {"name": detail["foodName"], "quantity": detail["count"]}
        for cart in info["foodInfo"].get("cartDetails", [])
        for detail in cart.get("details", [])
    ]
    return {
        "order_id": str(common["wm_order_id_view"]),
        "status": status,
        "store": basic["wmPoiName"],
        "user_paid": info["chargeInfo"]["userPayTotalAmount"],
        "items": items,
    }


def summarize_orders(payload):
    """Best-effort summary of every order in ``payload``.

    Only orders with status "待接单" or "待发起配送" are included.
    For "待发起配送" orders, only push when current time >= canClickButtonTime - 3 minutes.
    A single malformed order (missing field, broken nested JSON, unexpected
    shape) is skipped with a warning printed to stderr — it never aborts the
    whole collection. The raw response is archived separately, so even if every
    order fails to summarize, the original data is still preserved on disk.
    """
    summaries = []
    order_list = (payload.get("data") or {}).get("orderList") or []
    for index, order in enumerate(order_list):
        try:
            result = _summarize_one(order)
            if result is not None:
                summaries.append(result)
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
