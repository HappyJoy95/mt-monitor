# mt-monitor

本地美团闪购商家端订单采集工具。监控「待接单」状态，把完整原始接口响应存档到
`raw/`，并生成可读的订单摘要到 `data/`。

## 设计要点

美团订单接口需要动态签名 `mtgsig`，**无法从静态 cURL 重放**（很快 403，
即使 Cookie 仍有效）。因此本工具不伪造签名，而是连接你本机已登录美团的
浏览器，复用真实会话去「捕获」页面自己发出的请求响应。

## 目录与职责

- `src/mt_monitor/normalize.py`：从原始响应提取稳定摘要字段。
- `src/mt_monitor/storage.py`：时间戳归档原始 JSON、写入最新摘要（按状态分文件）。
- `src/mt_monitor/cli.py`：命令行入口（`import` / `pull`）。
- `src/mt_monitor/bridge.py`：CDP 桥接，连本机浏览器捕获订单响应。
- `src/mt_monitor/wechat_webhook.py`：企业微信机器人 webhook 客户端（URL 校验 + 文本发送）。
- `src/mt_monitor/notify.py`：把订单摘要格式化为文本并推送（默认不去重，每次都推）。
- `raw/`：完整接口响应（含敏感信息），仅本地保留，**不提交 Git**。
- `data/`：从原始数据派生的订单摘要。

## 安装

需要 Python 3.11+。

```bash
# 浏览器桥接依赖（仅 pull 子命令需要）
python -m pip install playwright
# 注意：不需要 `playwright install chromium`，因为连接的是本机已运行的浏览器
```

`import` 子命令只依赖 Python 标准库。`pull` 子命令在启用推送时还需要
`requests`（仅在真正发送时才导入，不影响采集本身）：

```bash
python -m pip install requests
```

## 启动带远程调试的 Edge 并登录

```bash
"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --remote-debugging-port=9222 \
  --user-data-dir="/tmp/mt-monitor-edge"
```

在打开的 Edge 中登录美团商家后台，并打开订单页：

```
https://shangoue.meituan.com/#/page/orderbusiness#/order/unprocessed
```

## 用法

### 从已有 JSON 文件导入（无需浏览器）

```bash
python -m src.mt_monitor.cli import raw/2026-07-30T14-57-00+0800-order-new.json
```

### 实时拉取（需要上面的 Edge 在运行且已登录）

```bash
python -m src.mt_monitor.cli pull
```

`pull` 通过 CDP 直接复用你已登录浏览器里的会话，动态签名 `mtgsig` 由浏览器实时
生成，无需任何配置文件或保存的认证数据。只要本机 Edge 开着远程调试并登录美团即可
首次直接使用。`pull` 只监控并拉取「待接单」状态并推送。拉取/导入后，只要有订单就会
自动推送到企业微信群机器人（纯文本，固定包含 **订单号 / 状态 / 门店** 三要素及
商品列表）。默认**不去重**，每次抓到都推，重复运行也照推（业务上无影响）。如需
去重可传 `dedup=True`。不想推送时加 `--no-notify`：

```bash
python -m src.mt_monitor.cli pull --no-notify
```

拉取结果：

- 原始响应：`raw/<时间戳>-order-list.json`
- 订单摘要：`data/latest-new-orders.json`

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 企业微信推送

拉取/导入后，只要有订单就会推送到企业微信群机器人（纯文本，固定包含
订单号 / 状态 / 门店 + 商品列表）。默认不去重，每次抓到都推送。

### 配置（二选一，均不提交 Git）
1. 新建 `config/notify`，内容仅为机器人的完整 URL（一行）：
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的KEY`
2. 或设置环境变量 `QYWECHAT_WEBHOOK` 指向同一 URL（优先级更高）。

`load_webhook_url` 会严格校验 URL 形态（必须 `https://qyapi.weixin.qq.com/
cgi-bin/webhook/send` 且含 `key`），避免误填。URL 含密钥，已被 `.gitignore`
排除（`config/notify`）。

### 关闭与验证
- 关闭单次推送：`pull` / `import` 加 `--no-notify`。
- 本地验证推送内容（不真实发送）：`process_notifications(orders, path, dry_run=True)`
  会打印将发送的文本而不发请求；`python -m unittest tests.test_notify` 覆盖
  格式化、缺失配置等行为（默认不去重，每次抓到都推）。

## 认证边界

登录态、Cookie 与 `mtgsig` 等认证数据绝不写入版本控制。
遇到 401/403，请在 Edge 中刷新或重新登录后重试，不要长期保存或伪造签名。

## 实现说明与已知限制

- 真实列表接口为 `/order/list/page/unprocessed`（POST）。美团订单页在**点击
  “待接单 / 进行中”标签页**时才请求该接口；单纯的页面刷新只发计数接口
  `/order/list/count` 与轮询接口 `/order/list/interval`（后者仅返回各状态订单
  计数，不含 `orderList`）。因此 bridge 通过点击目标状态标签页来触发列表请求，
  而非 reload。
- **页面是微前端 + iframe 架构**：订单标签页（`<button class="tab-btn_c17">`，
  文本形如 “待接单 0”）并不在顶层 `document` 里，而是位于 `id="hashframe"` 的
  iframe 内部；真实的 `/order/list/page/unprocessed` XHR 也由该 iframe 发出。
  因此 bridge 的点击必须落在 `page.frame(name="hashframe")` 上（响应监听仍用
  `page.expect_response`，它会覆盖子框架请求）。早期版本在主文档里点击导致永远
  点不到标签、拉不到列表，已修正。
- 标签文本带数量后缀（“待接单 0”），按精确相等匹配会失败，故用
  `button.tab-btn_c17` + 文本前缀定位，并保留“先点对面标签、再点目标标签”的逻辑，
  以保证即使目标标签已激活也能触发一次新请求。
- 真实端到端拉取需要在本机运行已登录的 Edge，CI 环境无法自动验证；遇到 401/403
  请刷新/重新登录，不要伪造 mtgsig。
