# 美团订单监控 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一个可用本地浏览器登录态拉取美团订单、归档完整原始响应并生成订单摘要的命令行项目。

**Architecture:** Python 标准库负责文件归档与摘要转换；浏览器自动化层只负责进入已登录美团页面并读取订单接口返回。每次运行先将响应以原样 JSON 写入 `raw/`，再由同一响应生成 `data/latest-orders.json`，确保摘要可回溯到原始数据。

**Tech Stack:** Python 3.11+、标准库、Playwright（浏览器会话与网络响应捕获）、unittest。

---

## File structure

- `src/mt_monitor/normalize.py`：从美团原始订单响应提取稳定摘要字段。
- `src/mt_monitor/storage.py`：创建目录、以时间戳归档原始 JSON、写入最新摘要。
- `src/mt_monitor/fetch.py`：启动持久化浏览器会话、访问订单页并捕获订单列表 JSON 响应。
- `src/mt_monitor/cli.py`：组合拉取、归档和摘要写入的命令行入口。
- `tests/test_normalize.py`：摘要字段与嵌套 JSON 解析测试。
- `tests/test_storage.py`：原始归档与摘要写入测试。
- `README.md`：安装、运行、数据文件位置与认证边界说明。

### Task 1: Create the pure data layer

**Files:**
- Create: `src/mt_monitor/normalize.py`
- Create: `tests/test_normalize.py`

- [ ] **Step 1: Write the failing normalization test**

```python
from src.mt_monitor.normalize import summarize_orders


def test_summarize_orders_reads_nested_order_strings():
    payload = {
        "data": {"orderList": [{
            "commonInfo": '{"wm_order_id_view": "123", "orderStatus": 2}',
            "orderInfo": '{"chargeInfo": {"userPayTotalAmount": 210.0}, "unifiedBasicInfo": {"wmPoiName": "测试门店", "orderStatusDesc": "待接单"}, "foodInfo": {"cartDetails": [{"details": [{"foodName": "测试商品", "count": 1}]}]}}',
        }]}
    }
    assert summarize_orders(payload) == [{
        "order_id": "123", "status": "待接单", "store": "测试门店",
        "user_paid": 210.0, "items": [{"name": "测试商品", "quantity": 1}],
    }]
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m unittest tests/test_normalize.py -v`

Expected: FAIL because `src.mt_monitor.normalize` does not exist.

- [ ] **Step 3: Implement the smallest normalizer**

```python
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
```

- [ ] **Step 4: Run the test and verify success**

Run: `python -m unittest tests/test_normalize.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mt_monitor/normalize.py tests/test_normalize.py
git commit -m "feat: normalize Meituan orders"
```

### Task 2: Add durable data storage

**Files:**
- Create: `src/mt_monitor/storage.py`
- Create: `tests/test_storage.py`

- [ ] **Step 1: Write the failing storage test**

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from src.mt_monitor.storage import save_snapshot


def test_save_snapshot_writes_raw_and_latest_summary():
    with TemporaryDirectory() as directory:
        raw_path, summary_path = save_snapshot(Path(directory), {"data": {}}, [])
        assert raw_path.parent.name == "raw"
        assert json.loads(raw_path.read_text()) == {"data": {}}
        assert json.loads(summary_path.read_text()) == []
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m unittest tests/test_storage.py -v`

Expected: FAIL because `src.mt_monitor.storage` does not exist.

- [ ] **Step 3: Implement timestamped, non-overwriting storage**

```python
import json
from datetime import datetime


def save_snapshot(project_root, payload, summary):
    raw_dir = project_root / "raw"
    data_dir = project_root / "data"
    raw_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S%z")
    raw_path = raw_dir / f"{timestamp}-order-list.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = data_dir / "latest-orders.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return raw_path, summary_path
```

- [ ] **Step 4: Run the test and verify success**

Run: `python -m unittest tests/test_storage.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mt_monitor/storage.py tests/test_storage.py
git commit -m "feat: archive raw order responses"
```

### Task 3: Capture a live order response and expose the command

**Files:**
- Create: `src/mt_monitor/fetch.py`
- Create: `src/mt_monitor/cli.py`
- Create: `README.md`

- [ ] **Step 1: Add a fetch function that returns one JSON order-list response**

```python
from playwright.sync_api import sync_playwright


def fetch_order_list(profile_dir, order_url):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            profile_dir, headless=False
        )
        with browser.expect_response(lambda response: "order" in response.url.lower()) as response_info:
            browser.new_page().goto(order_url, wait_until="domcontentloaded")
        response = response_info.value
        payload = response.json()
        browser.close()
        return payload
```

- [ ] **Step 2: Compose fetch, normalization and persistence in the CLI**

```python
from pathlib import Path
from .fetch import fetch_order_list
from .normalize import summarize_orders
from .storage import save_snapshot


def main():
    root = Path(__file__).resolve().parents[2]
    payload = fetch_order_list("data/edge-profile", "https://e.waimai.meituan.com/")
    raw_path, summary_path = save_snapshot(root, payload, summarize_orders(payload))
    print(f"原始数据：{raw_path}")
    print(f"订单摘要：{summary_path}")
```

- [ ] **Step 3: Document installation and data handling**

```markdown
python -m pip install playwright
python -m playwright install chromium
python -m src.mt_monitor.cli
```

State that `raw/` contains complete, potentially sensitive responses and must remain local; `.gitignore` excludes browser profile data but keeps raw response files available to the project owner.

- [ ] **Step 4: Run a live pull**

Run: `python -m src.mt_monitor.cli`

Expected: browser opens with the existing logged-in session, a complete raw JSON file is created under `raw/`, and `data/latest-orders.json` is created.

- [ ] **Step 5: Run all tests**

Run: `python -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src tests README.md .gitignore
git commit -m "feat: add Meituan order pull command"
```
