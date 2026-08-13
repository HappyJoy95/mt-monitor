# mt-monitor 交接说明

## 目标

建立本地美团闪购商家端订单采集工具：监控"待接单"状态，完整原始接口响应保存到
`raw/`，并生成可读订单摘要，支持推送到企业微信群。

## 项目位置

`/Users/ashui/Documents/mt-monitor`

## 功能概述

1. **订单采集**：通过 CDP 桥接本地已登录浏览器，捕获美团闪购「待接单」订单
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
# 拉取订单并推送
python -m src.mt_monitor.cli pull

# 拉取但不推送
python -m src.mt_monitor.cli pull --no-notify

# 拉取但只推主群，不推门店群
python -m src.mt_monitor.cli pull --no-store-notify

# 导入已有 JSON 文件
python -m src.mt_monitor.cli import raw/xxx.json
```

## 文件结构

```
config/
  notify              # 主推送 webhook URL（不提交 Git）
  store_webhooks.json # 门店映射表（不提交 Git）
src/mt_monitor/
  cli.py              # 命令行入口
  normalize.py        # 订单数据解析
  storage.py          # 数据归档
  notify.py           # 推送逻辑（主推送 + 门店推送）
  wechat_webhook.py   # 企业微信 webhook 客户端
  bridge.py           # CDP 浏览器桥接
raw/                  # 原始接口响应（不提交 Git）
data/                 # 订单摘要（不提交 Git）
tests/                # 测试文件
```

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## Git 状态注意事项

- 不要提交 `config/notify`、`config/store_webhooks.json`、`raw/`、`data/`
- `.DS_Store` 文件未跟踪，应保持忽略
