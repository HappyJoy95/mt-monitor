# mt-monitor 交接说明

## 目标

建立本地美团闪购商家端订单采集工具：监控“待接单”状态，完整原始接口响应保存到
`raw/`，并生成可读订单摘要。

## 项目位置

`/Users/ashui/Documents/mt-monitor`

## 已完成

- Git 仓库已初始化，当前在 `main`。
- `src/mt_monitor/normalize.py`：解析 `data.orderList`，将嵌套的 `commonInfo` 与 `orderInfo` JSON 转成订单摘要。
- `src/mt_monitor/storage.py`：原样、UTF-8、缩进 JSON 归档；`save_raw()` 写 `raw/`、
  `save_summary()` 写 `data/latest-new-orders.json`（导入走 `latest-orders.json`）。
- 测试通过：`python3 -m unittest discover -s tests -v`（当前共 2 个通过）。
- **不再使用静态 `session.json` 重放**：美团 `mtgsig` 是动态签名，复制的 cURL 很快
  403，因此改为本地 CDP 浏览器桥接（`bridge.py`），直接复用已登录 Edge 的会话实时
  捕获列表响应。原先只起误导作用的 `config/session.json` / `session.example.json`
  及其写入模块 `session.py` 已删除，避免长期落盘认证数据。
- 已成功通过 CDP 桥接采集一次「待接单」请求并保存原始响应：
  `raw/2026-07-30T14-57-00+0800-order-new.json`。

## 已验证接口

- URL：`https://shangoue.meituan.com/gw/api/unified/r/order/list/page/unprocessed`
- 方法：`POST`
- 公共查询参数：`ignoreSetRouterProxy=true`、`yodaReady=h5`、`csecplatform=4`、`csecversion=4.2.4`、`mtgsig=<动态签名>`。
- 关键请求头：`Cookie`、`Content-Type: application/x-www-form-urlencoded`、`Origin`、`Referer`、浏览器 `User-Agent`。
- 待接单请求体：`tag=order_new`（仅通过点击「待接单」标签页触发，由浏览器自行发出，
  脚本不构造请求体）。
- 进行中状态**当前不采集**（按需求只监控「待接单」）。
- 响应订单数组位置：`data.orderList`。

## 当前问题

`mtgsig` 是动态签名。把从浏览器复制的 cURL 在脚本中重放会很快得到 `403 Forbidden`，即使 Cookie 仍有效。因此长期可靠方案不能依赖静态 `session.json` 重放。

## 推荐实现：本地 CDP 浏览器桥接

在用户本机启动 Edge：

```bash
"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --remote-debugging-port=9222 \
  --user-data-dir="/tmp/mt-monitor-edge"
```

用户在该新 Edge 中登录美团并打开：

`https://shangoue.meituan.com/#/page/orderbusiness#/order/unprocessed`

调试页已验证可从 `http://127.0.0.1:9222/json` 发现订单标签页。下一个 IDE 代理应实现一个本地桥接脚本：

1. 通过 CDP 连接 `http://127.0.0.1:9222`，找到 URL 含 `shangoue.meituan.com` 与 `orderbusiness` 的 page target。
2. 使用该标签页的浏览器上下文触发“待接单”列表加载（通过点击「待接单」标签页，
   为保证即使目标标签已激活也能触发一次新请求，会先点「进行中」再点回「待接单」；
   其响应被捕获并保留，对面的响应丢弃）。
3. 监听 Network response，筛选路径 `/gw/api/unified/r/order/list/page/unprocessed`。
4. 读取对应 JSON response body；先调用 `save_raw()` 无条件保存完整响应，再调用
   `summarize_orders()` 尽力生成摘要（单笔异常只跳过该笔，不中断整次采集）。
5. 用 `summarize_orders()` 生成摘要，写入 `data/latest-new-orders.json`。
6. 若接口返回 401/403，输出“请在 Edge 中刷新/重新登录后重试”，不要尝试伪造或长期保存 `mtgsig`。

建议技术：Node.js + `chrome-remote-interface` 或 Playwright 的 `connectOverCDP`。浏览器由用户本机运行，真实 Cookie 与签名绝不能写入 Git。

## 待补文件

- `src/mt_monitor/cli.py`：命令行入口；已有未提交的 `tests/test_cli.py` 为导入 JSON 的草稿测试，可保留或调整。
- `src/mt_monitor/bridge.*`：CDP 连接、网络响应捕获、状态切换。
- `README.md`：安装依赖、启动 Edge、登录、运行单次拉取、查看原始数据。

## Git 状态注意事项

- 已有提交：`9e47fd9`（设计/计划）、`242c789`（解析）、`e171ff4`（归档）、`bad43d7`（归档可读性测试）。
- `tests/test_cli.py` 是未提交草稿。
- `.DS_Store` 文件未跟踪，应保持忽略。
- 不要提交 `config/notify`、`config/store_webhooks.json`、`raw/`、`data/`。
