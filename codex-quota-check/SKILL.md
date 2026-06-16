---
name: codex-quota-check
description: 查询 Codex 剩余额度（5小时/每周）— 通过本机 codex app-server JSON-RPC，不抓网页
category: mlops
---

# Codex Quota Check

通过本机 `codex app-server --listen stdio://` 走 JSON-RPC 查询 Codex 使用额度，返回剩余百分比和重置时间。

## 触发方式

当用户说以下任意语句时，调用此 skill：
- "查询 codex 剩余额度"
- "codex 余量"
- "codex quota"
- "看看 codex 还剩多少"

## 执行步骤

### 1. 运行脚本

```bash
node /root/.hermes/skills/codex-quota-check/scripts/codex-quota.mjs
```

输出为 JSON 格式，示例：
```json
{
  "planType": "plus",
  "rateLimitReachedType": null,
  "credits": { "hasCredits": false, "unlimited": false, "balance": "0" },
  "buckets": [
    { "name": "5h", "key": "primary", "usedPercent": 1, "remainingPercent": 99,
      "windowDurationMins": 300, "resetsAt": 1781239989, "resetsAtISO": "2026-06-12T04:53:09.000Z" },
    { "name": "weekly", "key": "secondary", "usedPercent": 13, "remainingPercent": 87,
      "windowDurationMins": 10080, "resetsAt": 1781242942, "resetsAtISO": "2026-06-12T05:42:22.000Z" }
  ]
}
```

### 2. 格式化输出（格式硬性要求）

将 JSON 解析后，按以下**硬性要求**格式化：

- **日期**：两个 bucket 每次都必须带日期，格式 `MM/DD HH:mm`
- **时区**：必须用北京时间（UTC+8），标注 `(北京时间)`
- **百分比**：`remainingPercent = clamp(100 - usedPercent, 0, 100)`

```markdown
🔋 **Codex 额度**

**5 小时额度** — 剩余 99% · 已用 1%
  🕐 重置：06/12 12:53 (北京时间)

**周额度** — 剩余 87% · 已用 13%
  🕐 重置：06/12 13:42 (北京时间)

📋 套餐：Plus · 无信用额度
```

### 3. 输出展示规则

| 场景 | 格式 |
|------|------|
| Hermes WebUI（Markdown） | 用 `**粗体**`、emoji、表格，完整展示 |
| Telegram | 同上，保持简洁紧凑，单条消息不超过 4096 字符 |
| JSON 输出（给脚本/状态栏） | 直接返回原始 JSON |

### 4. 错误处理

- 如果脚本返回 `{ "error": "..." }`，展示错误信息并建议检查 Codex 是否安装
- 如果 `buckets` 数组为空，说明未返回额度数据
- 如果 `rateLimitReachedType` 非 null，用 ⚠️ 强调提示

## 协议说明

### 连接流程

```
→ { "method": "initialize", "id": 1, "params": { "clientInfo": { ... } } }
→ { "method": "initialized" }
→ { "method": "account/rateLimits/read", "id": 2 }
← { "result": { ... } }
```

### 窗口识别

| windowDurationMins | 名称 |
|--------------------|------|
| 300 | 5小时额度 |
| 10080 | 周额度 |
| 其他 | `${mins}min` |

### 字段优先级

`rateLimitsByLimitId.codex` > `rateLimits`（旧格式）

## 工程注意事项

1. **不要每次冷启动** — 本脚本每次独立 spawn app-server，适合手动查询。常驻场景需做缓存（30-60s）
2. **展示 reset 时间** — 必须带日期（MM/DD）和北京时间标注
3. **字段容错** — secondary/credits/primary 都可能为 null
4. **以本机 schema 为准** — `codex app-server generate-json-schema --out DIR`
