# mt-monitor 交接说明

## 目标

建立本地美团闪购商家端订单采集工具：监控"待接单"状态，完整原始接口响应保存到
`raw/`，并生成可读订单摘要，支持推送到企业微信群。

## 项目位置

`~/vibe-coding/mt-monitor`（2026-09-15 从 `~/Documents/ds-chat` 体系迁入。
迁移时排除了 `__pycache__`；如需重建环境见下方）

## 功能概述

1. **订单采集**：通过 CDP 桥接本地已登录浏览器，点「进行中」标签捕获全状态订单列表，
   再由 `normalize` 过滤出「待接单」+「待发起配送」（后者需进入拣货完成前 6 分钟窗口）
2. **主推送**：所有订单推送到主企业微信群（`config/notify` 或 `QYWECHAT_WEBHOOK` 环境变量）
3. **门店推送**：按门店名精确匹配，推送到对应门店群（`config/store_webhooks.json`）

## 门店映射表处理

### 映射表格式

映射表为 Excel 文件（.xlsx），列结构：

| 列名 | 说明 | 示例 |
|------|------|------|
| 门店名 | 与订单 `store` 字段精确匹配 | 华为授权体验店（悦荟广场店） |
| 营业开始时间 | HH:MM:SS 格式 | 09:30:00 |
| 营业结束时间 | HH:MM:SS 格式（支持跨午夜） | 22:00:00 |
| webhook | 企业微信群机器人 URL | https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=... |

### 处理步骤

拿到映射表后，执行以下操作：

1. **读取 Excel 文件**：使用 pandas 或 openpyxl 读取映射表
   ```python
   import pandas as pd
   df = pd.read_excel('映射表.xlsx')
   ```

2. **转换为 JSON 格式**：将 DataFrame 转为 JSON 数组
   ```python
   import json
   records = df.to_dict(orient='records')
   # 确保列名与代码一致：门店名、营业开始时间、营业结束时间、webhook
   ```

3. **写入配置文件**：保存到 `config/store_webhooks.json`
   ```python
   with open('config/store_webhooks.json', 'w', encoding='utf-8') as f:
       json.dump(records, f, ensure_ascii=False, indent=2)
   ```

4. **验证 webhook 格式**：每个 webhook URL 必须以 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=` 开头

5. **发送部署确认**：对每个门店调用 `send_store_deployment_message()` 发送确认消息
   ```python
   from src.mt_monitor import notify
   for store in records:
       notify.send_store_deployment_message(
           webhook_url=store['webhook'],
           store_name=store['门店名'],
           start_time=store['营业开始时间'],
           end_time=store['营业结束时间'],
       )
   ```

### 注意事项

- **门店名必须精确匹配**：订单的 `store` 字段与映射表的 `门店名` 完全一致才会推送
- **营业时间校验**：仅在营业时间内推送，非营业时间跳过（主推送不受影响）
- **跨午夜支持**：营业时间可跨午夜（如 22:00-06:00）
- **不要提交映射表到 Git**：`config/store_webhooks.json` 包含敏感信息，已在 `.gitignore` 中排除

## CLI 命令

```bash
# 拉取订单并推送（页面卡死会自动刷新重试，默认最多 3 次尝试）
python -m src.mt_monitor.cli pull

# 拉取但不推送
python -m src.mt_monitor.cli pull --no-notify

# 拉取但只推主群，不推门店群
python -m src.mt_monitor.cli pull --no-store-notify

# 调整自愈重试次数（0 = 不重试）
python -m src.mt_monitor.cli pull --retries 0

# 常驻守护：每 60s 拉一次，卡死自愈，连续失败 3 次推企微告警
python -m src.mt_monitor.cli watch

# 只跑一轮（自检）
python -m src.mt_monitor.cli watch --once

# 连续失败 5 次就退出（交给 launchd 拉起）；告警阈值 2 次
python -m src.mt_monitor.cli watch --max-failures 5 --alert-after 2

# 导入已有 JSON 文件
python -m src.mt_monitor.cli import raw/xxx.json
```

退出码：`0` 正常；`1` 拉取失败 / 失败达 `--max-failures`；`3` 缺依赖。

## 卡死自愈（2026-09-16 新增）

浏览器卡死（白屏、转圈、点了没反应）时 `pull` 会自己刷新重试，阶梯为
软刷新 → 硬刷新（忽略缓存）→ 重新导航，每次刷新后等页面真就绪（readyState
complete + `hashframe` iframe 挂上 + 目标标签可点）再重新点标签。

**不重试的两种情况**（刷新无效，立即报错让人工处理）：页面跳到登录页、
接口 `code` 为 401/403。

失败现场落盘 `data/last-pull-error.json`（url/title/readyState/attempts/error）。
新增测试：`tests/test_bridge.py`（自愈策略）、`tests/test_watch.py`（守护与告警）。

### 目标标签与订单范围（跨机器约定）

`bridge.TARGET_TAB = "进行中"`（该列表含各状态订单），实际监控哪些状态由
`normalize.VALID_STATUSES = {"待接单", "待发起配送"}` 决定；「待发起配送」还要满足
`canClickButtonTime - 6 分钟`（`PICK_READY_OFFSET_MINUTES`，2026-09-17 由 3 分钟调大）
才推送。`tests/test_bridge.py::TabStrategyTests` 把这个
约定钉住了——改 `TARGET_TAB` 会静默缩小监控范围，别当成实现细节随手改。

### 定位逻辑的三个硬约束（真机实测，别再踩）

1. **标签按钮 class 带构建 hash**：见过 `tab-btn_c17` 和 `tab-btn_c17d4`。必须用前缀
   选择器 `button[class*="tab-btn_"]`，写全名会在美团发版后静默失效。
2. **就绪探针要和点击同语义**：点击是 `locator(sel, has_text=label).first`（先筛文本
   再取第一个），所以探针必须 `querySelectorAll` 后 `find(文本包含 label)`；
   用 `querySelector` 只会拿到「全部」，永远判为未就绪 → 每轮白等 20 秒。
3. **先注册响应监听再点击**：页面 0.05–0.3 秒就返回，`expect_response` 是先点击后
   注册，会漏掉。用 `page.on("response", cb)`，用完 `remove_listener`。

另外：当前选中标签带 `active_<hash>` 前缀，用它决定是否先点对面标签（目标已激活时
不先切走，SPA 不会重新请求）。正常一轮 pull ≈ 3 秒；明显变慢=定位逻辑失效的早期信号。

## 文件结构

```
config/
  notify              # 主推送 webhook URL（不提交 Git）
  store_webhooks.json # 门店映射表（不提交 Git）
src/mt_monitor/
  cli.py              # 命令行入口（import / pull / watch）
  normalize.py        # 订单数据解析
  storage.py          # 数据归档
  notify.py           # 推送逻辑（主推送 + 门店推送）
  wechat_webhook.py   # 企业微信 webhook 客户端
  bridge.py           # CDP 浏览器桥接 + 页面卡死自愈
  watch.py            # 常驻守护循环（定时拉取 + 告警）
raw/                  # 原始接口响应（不提交 Git）
data/                 # 订单摘要 + last-pull-error.json（不提交 Git）
tests/                # 测试文件
```

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## Git 状态注意事项

- 不要提交 `config/notify`、`config/store_webhooks.json`、`raw/`、`data/`
- `.DS_Store` 文件未跟踪，应保持忽略
