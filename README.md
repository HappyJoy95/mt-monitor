# mt-monitor

本地美团闪购商家端订单采集工具。监控「待接单」与「待发起配送」两种状态的订单
（后者需进入拣货完成前 3 分钟窗口才推送），把完整原始接口响应存档到 `raw/`，
并生成可读的订单摘要到 `data/`。

## 设计要点

美团订单接口需要动态签名 `mtgsig`，**无法从静态 cURL 重放**（很快 403，
即使 Cookie 仍有效）。因此本工具不伪造签名，而是连接你本机已登录美团的
浏览器，复用真实会话去「捕获」页面自己发出的请求响应。

## 目录与职责

- `src/mt_monitor/normalize.py`：从原始响应提取稳定摘要字段。
- `src/mt_monitor/storage.py`：时间戳归档原始 JSON、写入最新摘要（按状态分文件）。
- `src/mt_monitor/cli.py`：命令行入口（`import` / `pull` / `watch`）。
- `src/mt_monitor/bridge.py`：CDP 桥接，连本机浏览器捕获订单响应；**页面卡死时自愈刷新重试**。
- `src/mt_monitor/watch.py`：常驻守护循环（定时拉取、连续失败告警）。
- `src/mt_monitor/wechat_webhook.py`：企业微信机器人 webhook 客户端（URL 校验 + 文本发送）。
- `src/mt_monitor/notify.py`：把订单摘要格式化为文本并推送（默认不去重，每次都推）。
- `raw/`：完整接口响应（含敏感信息），仅本地保留，**不提交 Git**。
- `data/`：从原始数据派生的订单摘要；`data/last-pull-error.json` 是最近一次失败的现场诊断。

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
首次直接使用。`pull` 拉取「进行中」标签的订单列表（含全部状态），再由
`normalize` 过滤出「待接单」与「待发起配送」两种状态并推送。拉取/导入后，只要有订单就会
自动推送到企业微信群机器人（纯文本，固定包含 **订单号 / 状态 / 门店** 三要素及
商品列表）。默认**不去重**，每次抓到都推，重复运行也照推（业务上无影响）。如需
去重可传 `dedup=True`。不想推送时加 `--no-notify`：

```bash
python -m src.mt_monitor.cli pull --no-notify
```

拉取结果：

- 原始响应：`raw/<时间戳>-order-list.json`
- 订单摘要：`data/latest-new-orders.json`

### 页面卡死自愈（`pull` 默认开启）

长开的商家后台标签页会卡：白屏、一直转圈，或者页面看着正常但**点击标签不再发请求**。
这三种症状脚本能观察到的表现是同一个——等不到 `/order/list/page/unprocessed` 响应。
既然手动 F5 是已验证的解药，`pull` 就自己刷：

1. 第 1 次抓取失败（超时 / 响应非 JSON / 响应里没有 `orderList`）→ **软刷新** `page.reload()`
2. 第 2 次仍失败 → **硬刷新**（CDP `Page.reload(ignoreCache=True)`，治白屏缓存态）
3. 第 3 次仍失败 → **重新导航**到订单页 URL

每次刷新后不是立刻点标签，而是等页面**真就绪**：`document.readyState === "complete"`
且 `hashframe` iframe 已挂上、目标标签按钮可见可点。就绪判定通过才重新点标签，
避免"刷新没加载完就点、点了没反应"这种假失败。默认最多 3 次尝试（`--retries 2`），
每次尝试的等待有独立超时，不会因为渲染进程无响应而永久阻塞。

**两种情况不重试**，因为它们刷新也没用，重试只会浪费时间并给出误导性报错：

| 情况 | 判定依据 | 行为 |
|------|----------|------|
| 登录态失效 | 页面跳到登录流程（passport / 登录 URL 标记）或页面出现密码框 | 立即报错「请重新登录」 |
| 接口返回要登录 | 响应 `code` 为 401/403 | 立即报错「请重新登录」 |

失败时会把现场写进 `data/last-pull-error.json`：

```json
{
  "at": "2026-09-16T20:41:02+08:00",
  "stage": "capture",
  "attempts": 3,
  "recoveries": 2,
  "error": "超时未捕获到订单列表接口响应（页面可能已卡死或未加载完）",
  "page": {"url": "...", "title": "...", "readyState": "complete", "hashframe": true, "tab": "no-tab"}
}
```

### 常驻守护（`watch`）

无人值守时用 `watch`，它把「定时拉 + 卡死自愈 + 失败告警」合成一个进程：

```bash
python -m src.mt_monitor.cli watch                      # 每 60s 拉一次，一直守着
python -m src.mt_monitor.cli watch --interval 30        # 每 30s
python -m src.mt_monitor.cli watch --once               # 只跑一轮就退出（自检用）
python -m src.mt_monitor.cli watch --max-failures 5     # 连续失败 5 次就退出（交给 launchd 拉起）
python -m src.mt_monitor.cli watch --alert-after 3      # 连续失败 3 次推企微告警（0 = 关闭）
python -m src.mt_monitor.cli watch --retries 3          # 每轮内部刷新重试 3 次
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--interval` | 60 | 两次拉取间隔秒数 |
| `--retries` | 2 | 每轮内部的自愈重试次数（总尝试 = retries + 1） |
| `--max-failures` | 0 | 连续失败多少次后退出；**0 = 永不退出** |
| `--alert-after` | 3 | 连续失败多少次后推企微告警；0 = 关闭告警 |
| `--once` | — | 只跑一轮 |
| `--cdp` / `--timeout` / `--no-notify` / `--no-store-notify` | 同 `pull` | |

行为约定：

- **只统计连续失败**：中间成功一次就清零，所以偶尔抽风不会导致守护退出。
- 连续失败达到 `--alert-after` 推一条告警到**主群**（走 `config/notify` / `QYWECHAT_WEBHOOK`）；
  告警发送失败只记日志，绝不影响采集。
- 连续失败达到 `--max-failures` 时发一条 🛑 退出告警，进程以退出码 1 结束——
  这样 launchd 之类的守护能感知异常并拉起，或提醒人工介入。
- 每轮重新连 CDP，不持有浏览器状态：中途重启 Edge 不影响下一轮。
- `Ctrl-C` / `SIGTERM` 干净退出（打印汇总后返回）。

`watch` 与 `pull` 退出码：`0` 正常；`1` 拉取失败或失败达上限；`3` 缺依赖。

### 浏览器看门狗（`edge-watch`）

上面那套自愈只能救**页面**。如果**浏览器进程本身假死**——9222 端口还在监听、但 CDP
握手一直超时——每轮 `pull` 都会卡在第一步就失败，自愈阶梯根本没机会执行。
2026-09-16 本机就这样丢了约 12 小时监控（182 轮全部 `connect_over_cdp: Timeout
180000ms exceeded`，20:38 才自行恢复）。

`edge-watch` 定期探测 CDP，连续 N 次失败后**只重启监控专用 profile 的 Edge 实例**
（按 `--user-data-dir` 精确匹配进程，绝不碰你正常浏览的 Edge），并推一条企微告警：

```bash
python -m src.mt_monitor.cli edge-watch                 # 探测一次（健康时秒回）
python -m src.mt_monitor.cli edge-watch --threshold 3   # 连续 3 次失败才重启（默认）
python -m src.mt_monitor.cli edge-watch --no-alert      # 只重启不告警
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--cdp` | `http://127.0.0.1:9222` | CDP 地址；端口也用于重启时拉起浏览器 |
| `--profile-dir` | `C:\tmp\mt-monitor-edge` | 只重启用该 user-data-dir 的实例 |
| `--edge-exe` | 自动探测 | Edge 可执行文件路径 |
| `--threshold` | 3 | 连续探测失败多少次后重启 |
| `--probe-timeout` | 5 | 单次探测超时秒数（假死的浏览器会接受 TCP 但不回包，必须短超时） |
| `--startup-timeout` | 60 | 重启后等待 CDP 就绪的秒数 |
| `--no-alert` | — | 重启后不推企微告警 |

行为约定：

- 计数存在 `data/edge_watch_state.json`，所以 `--threshold` 的含义是"**连续 N 次检查**"
  而不是"一个进程内探测 N 次"。
- 探测恢复正常即清零；**重启尝试过也清零**，把重试间隔摊到阈值周期上，
  避免浏览器一旦起不来就每轮都被杀掉重启。
- 退出码：`0` 正常（含"正在计数"）；`1` 已重启但 CDP 仍没起来（交给计划任务/人工介入）。
- 本机计划任务 `MT Edge Watch`：08:25–22:05（比拉取窗口 08:30–22:00 略宽）、每 5 分钟一次，
  输出到 `logs/edge-watch-YYYY-MM-DD.log`。
- 若重启后 Edge 掉登录，`pull` 会明确报"跳登录页/请重新登录"，需人工扫码一次。
- 验证重启路径不必动生产浏览器：用一次性 profile 与端口演练即可，例如
  `edge-watch --cdp http://127.0.0.1:9333 --profile-dir C:\tmp\mt-edge-selftest --threshold 1`
  （探测 9333 失败 → 杀掉该 profile 的进程 → 重新拉起 → 校验 CDP 就绪）。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 定时抓取与运行日志（Windows）

本机通过计划任务 `MT Monitor` 无人值守运行：每天 08:30 起、间隔 1 分钟、持续
13h30m（即 08:30–22:00），动作是 `run_pull.cmd`。

- `run_pull.cmd`：包装脚本，把每次运行写入 `logs/pull-YYYY-MM-DD.log`，每轮记录
  开始时间与 `exit=` 退出码，便于事后判断"哪一分钟没执行/失败"。
  **该文件必须保持 CRLF 换行**（cmd.exe 遇到 LF 换行会解析错乱），且保持纯 ASCII。
- 计划任务的 `MultipleInstancesPolicy` 为 `Parallel`：单轮耗时可能超过 1 分钟
  （推送较多订单时），`IgnoreNew` 会整分钟跳过，`Parallel` 则不会漏。
  Windows 计划任务的重复间隔**最小为 1 分钟**，无法配置 30 秒。
- `logs/` 已在 `.gitignore` 中排除。

### 溯源：哪一分钟没执行、为什么

`audit` 子命令把监控窗口内每一分钟与 `raw/` 抓取记录、`logs/pull-*.log` 运行
记录逐条对齐：

```bash
# 默认核对今天；也可指定日期
python -m src.mt_monitor.cli audit
python -m src.mt_monitor.cli audit --date 2026-09-14
```

输出示例（退出码：无缺失为 0，有缺失为 1，便于接告警）：

```
日期：2026-09-14   窗口：08:30 ~ 15:53（444 分钟）
成功抓取：442 分钟（其中 4 分钟有重复抓取）
计划运行：17 轮，失败 0 轮
运行日志：...\logs\pull-2026-09-14.log
缺失：2 分钟
  11:21  运行失败  exit=1 拉取失败：无法连接浏览器 CDP（http://127.0.0.1:9222）
  11:23  未运行  任务未触发（计划任务/机器状态）
```

每分钟的判定含义：

| 判定 | 含义 |
|------|------|
| 运行失败 | 该分钟有运行记录但退出码非 0，并引用进程打印的原因 |
| 未运行 | 该分钟既无抓取也无运行记录 → 任务未触发（调度/机器状态） |
| 上一轮仍在运行 | 该分钟被一个更早开始、尚未结束的轮次覆盖 |
| 运行成功但无输出 | 退出码 0 却没落盘 → 查磁盘/权限 |
| 运行中 | 该轮尚未结束（报告生成时仍在跑） |
| 无日志记录 | 日志尚未启用（`run_pull.cmd` 部署前）或该日无日志，无法判定 |

需要更底层的事件时，再看"任务计划程序"操作日志
（`Microsoft-Windows-TaskScheduler/Operational`，已启用）：`100` 任务启动、
`200/201` 动作开始/结束、`102` 任务结束。

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

### 门店群推送

除主推送外，支持按门店名将订单推送到对应门店的企业微信群。门店映射表配置在
`config/store_webhooks.json`（不提交 Git），格式：

```json
[
  {
    "门店名": "华为授权体验店（悦荟广场店）",
    "营业开始时间": "09:30:00",
    "营业结束时间": "21:00:00",
    "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
  }
]
```

- 订单的 `store` 字段与 `门店名` 精确匹配时，会额外推送到该门店的群。
- 推送仅在 `营业开始时间` ~ `营业结束时间` 内执行（支持跨午夜，如 22:00-06:00）。
- 未映射的门店或非营业时间内，跳过门店群推送（主推送不受影响）。

### 关闭与验证
- 关闭所有推送：`pull` / `import` 加 `--no-notify`。
- 仅关闭门店群推送：`pull` / `import` 加 `--no-store-notify`（主推送不受影响）。
- 本地验证推送内容（不真实发送）：`process_notifications(orders, path, dry_run=True)`
  会打印将发送的文本而不发请求；`python -m unittest tests.test_notify` 覆盖
  格式化、缺失配置等行为（默认不去重，每次抓到都推）。

## 认证边界

登录态、Cookie 与 `mtgsig` 等认证数据绝不写入版本控制。
遇到 401/403 或页面跳到登录页时，工具会**立即停止**并提示重新登录（不浪费刷新次数，
也不伪造签名），请在 Edge 中人工登录后重试。

## 实现说明与已知限制

- 真实列表接口为 `/order/list/page/unprocessed`（POST）。美团订单页在**点击状态
  标签页**时才请求该接口；单纯的页面刷新只发计数接口 `/order/list/count` 与轮询
  接口 `/order/list/interval`（后者仅返回各状态订单计数，不含 `orderList`）。因此
  bridge 通过点击目标状态标签页来触发列表请求，而非 reload。
- **目标标签是「进行中」**（`TARGET_TAB`）：该列表包含各状态的订单，订单范围由
  `normalize` 的 `VALID_STATUSES` + 拣货时间窗口决定（跨机器约定，有测试钉住）。
  当页面已停在该标签时，先点 `FALLBACK_TAB`（「待接单」）再点回来，否则 SPA 不会
  重新请求。
- **页面是微前端 + iframe 架构**：订单标签页位于 `id="hashframe"` 的 iframe 内部，
  真实的 `/order/list/page/unprocessed` XHR 也由该 iframe 发出。因此点击必须落在
  `page.frame(name="hashframe")` 上。早期版本在主文档里点击导致永远点不到标签，已修正。
- **标签按钮的 class 名带构建 hash，会随美团发版变化**（实测见过 `tab-btn_c17` 与
  `tab-btn_c17d4` 并存）。因此定位用**前缀匹配** `button[class*="tab-btn_"]`，不要写死
  全名——写死会在某次发版后静默失效（点到假元素 / 永远超时）。
- **就绪探针必须与点击定位语义一致**：`locator(selector, has_text=label).first` 是
  "先按文本筛选、再取第一个"，所以 JS 探针要 `querySelectorAll(...)` 后 `find(文本包含)`
  ——用 `querySelector` 只会命中标签条第一个元素（「全部」），永远是 `label-mismatch`。
- **响应要在点击前就注册监听**：页面常在 ~0.05–0.3s 内就返回，`expect_response`
  是"先点击、再注册等待"，会漏掉快速响应。正确做法是先 `page.on("response", ...)`
  再点击（用完记得 `remove_listener`）。
- **当前选中标签带 `active_<hash>`**（前缀一样会变）。据此判断要不要先切走：
  目标已激活时不先切到别的标签，SPA 不会重新请求。
- 标签文本带数量后缀（“进行中 1”“待接单 0”），按精确相等匹配会失败，故用
  `has_text` 子串匹配。
- 点击自带**短超时 + 最多 3 次重试**（只重试 `TimeoutError` 这类可操作性竞态；
  其他异常直接上抛给自愈逻辑判断），因为重试一次点击比触发一次整页刷新便宜得多。
- 实测耗时参考：正常一轮 pull ≈ 3 秒；探针/选择器写错时会白等满就绪超时（20 秒起），
  所以"pull 明显变慢"通常是定位逻辑失效的**早期信号**，值得当成告警看。
- 自愈与守护的决策逻辑**不依赖真实浏览器即可测试**：`tests/test_bridge.py` 用假
  Playwright 对象驱动 `pull_order_list`，覆盖“卡死→刷新→成功”“连续失败抬头”
  “掉登录不重试”“拿不到 orderList”“CDP 连不上”等路径；`tests/test_watch.py`
  覆盖连续失败计数、告警与退出条件。
- 真实端到端拉取需要在本机运行已登录的 Edge，CI 环境无法自动验证；遇到 401/403
  请刷新/重新登录，不要伪造 mtgsig。**整个 Edge 进程假死（CDP 都连不上）时脚本无法
  自救**，只能报「无法连接浏览器 CDP」，需强制退出后重新启动带远程调试的 Edge。
- 遥测/自检手段：`watch --once` 跑一轮并打印全过程（含自愈事件）；失败现场见
  `data/last-pull-error.json`。
