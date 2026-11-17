# AI 客服接口文档（/cs）

**Base URL：** `http://<host>:<api_port>`
**Content-Type：** `application/json`
**字符编码：** UTF-8
**默认端口：** `8000`（可通过配置项 `api_port` 或环境变量 `API_PORT` 覆盖）

---

## 目录

- [0. 服务端口配置](#0-服务端口配置)
- [1. 普通对话](#1-普通对话)
- [2. 主动触达](#2-主动触达)
- [3. 调试信息](#3-调试信息)
- [附录 A：Persona 字段说明](#附录-a-persona-字段说明)
- [附录 B：错误码](#附录-b-错误码)
- [附录 C：完整请求示例](#附录-c-完整请求示例)

---

## 0. 服务端口配置

服务监听地址来自配置：

- `api_host`（默认 `0.0.0.0`）
- `api_port`（默认 `8000`）

常见启动方式示例：

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

如果你在测试/生产环境使用了非 `8000` 端口，请将本文所有示例中的 `8000` 替换为实际端口（例如 `9000`）。

---

## 1. 普通对话

### `POST /cs`

同步返回完整回复，适合大多数接入场景。

流程：情绪识别 → 意图识别 → 工具/RAG → 生成回复

#### 请求体

```json
{
  "message": "string",
  "session_id": "string",
  "user_id": "string",
  "bot_id": "string",
  "collections": ["string"],
  "persona": {}
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | string | ✅ | 用户消息，1~2048 字符 |
| `session_id` | string | ❌ | 会话 ID，用于保持多轮对话上下文，最长 128 字符。默认 `"default"` |
| `user_id` | string | ❌ | 用户 ID，查询订单/物流时传入，最长 128 字符 |
| `bot_id` | string | ❌ | 机器人配置 ID。传入后从后台加载对应角色和知识库配置，优先级高于 `persona` / `collections` |
| `collections` | string[] | ❌ | 知识库集合列表，支持多库并发检索。未传入时使用 bot_id 配置或默认 `products` |
| `persona` | object | ❌ | 自定义角色配置，传入后完全替换默认角色。与 `bot_id` 同时传入时以 bot 配置为基础做增量覆盖。字段见[附录 A](#附录-a-persona-字段说明) |

> **角色优先级：** `bot_id` 加载的配置 > `persona` 字段 > 系统默认角色

**`collections` 可用值：**

| 值 | 知识库 |
|----|--------|
| `products` | 商品资料（默认） |
| `faq` | FAQ 问答 |
| `sop` | 流程 SOP |
| `policy` | 政策文件 |
| `agent_memory` | 通用记忆库 |
| `moments_knowledge` | 朋友圈评论话术 |
| `cs_knowledge` | 客服知识库 |
| `sales_script` | 销售话术库 |

#### 响应体

```json
{
  "reply": "string",
  "reply_parts": ["string"],
  "session_id": "string",
  "need_human_transfer": false,
  "transfer_reason": "emotion",
  "intent": "string",
  "emotion": "string",
  "emotion_polarity": "neutral",
  "trial_used": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `reply` | string | 完整回复文本，用于兼容旧客户端或不需要分条的场景 |
| `reply_parts` | string[] | 分条消息列表。模拟人类客服分多条发送的效果，前端可按顺序逐条渲染，建议每条间隔 500~800ms。若回复无需分条则列表仅含一项 |
| `session_id` | string | 会话 ID（与请求一致） |
| `need_human_transfer` | boolean | 是否需要转人工（情绪激动或售后类触发） |
| `transfer_reason` | string \| null | 转人工原因：`emotion`（情绪激动）/ `after_sales`（退货退款投诉，通常 `reply` 与 `reply_parts` 为空，由 CRM 发话） |
| `intent` | string | 识别到的意图：`order` / `knowledge` / `chitchat` / `unknown` |
| `emotion` | string | 识别到的情绪：`calm` / `excited` / `angry` |
| `emotion_polarity` | string | 情绪极性：`positive` / `neutral` / `negative` |
| `trial_used` | boolean \| null | 检测到客户已使用体验装时为 `true`，供 CRM 状态机切换场景 |

#### 示例

**最简请求：**
```bash
curl -X POST http://localhost:<api_port>/cs \
  -H "Content-Type: application/json" \
  -d '{
    "message": "我的包裹到哪里了？",
    "session_id": "user-001",
    "user_id": "member-10086"
  }'
```

**响应：**
```json
{
  "reply": "好的，我帮您查一下。您的订单已发货，现在在广州转运中心，预计明天可以送达。\n有其他问题随时告诉我。",
  "reply_parts": [
    "好的，我帮您查一下。您的订单已发货，现在在广州转运中心，预计明天可以送达。",
    "有其他问题随时告诉我。"
  ],
  "session_id": "user-001",
  "need_human_transfer": false,
  "intent": "order",
  "emotion": "calm"
}
```

**指定知识库和自定义角色：**
```bash
curl -X POST http://localhost:<api_port>/cs \
  -H "Content-Type: application/json" \
  -d '{
    "message": "有没有适合干皮的产品？",
    "session_id": "user-001",
    "collections": ["products", "faq"],
    "persona": {
      "name": "小新",
      "role": "护肤顾问",
      "brand": "新肌秀",
      "brand_intro": "专注天然温和护肤，旗下舒净雅香沐浴乳采用皂基+温和表活复配清洁体系",
      "personality_traits": ["专业自信", "亲切温柔"],
      "style_guidelines": ["回复简洁有力", "不编造产品功效"],
      "accuracy_rules": {
        "deny": "禁止介绍知识库以外的产品",
        "priority": "所有产品信息以知识库为最高优先级"
      }
    }
  }'
```

**使用已配置的机器人：**
```bash
curl -X POST http://localhost:<api_port>/cs \
  -H "Content-Type: application/json" \
  -d '{
    "message": "你好",
    "session_id": "user-001",
    "bot_id": "xinji-cs-bot"
  }'
```

---

## 2. 主动触达

### `POST /cs/outreach`

由 CRM 状态机调用，AI 主动发出首条消息（不响应客户输入）。

#### 请求体

```json
{
  "user_id": "string",
  "session_id": "string",
  "scene": "trial_not_used",
  "journey_stage": 1,
  "intent_level": 80,
  "trial_received": true,
  "trial_used": false,
  "order_count": 1,
  "days_since_last_order": 14,
  "consecutive_no_reply": 2,
  "in_low_freq_pool": false,
  "customer_profile": "客户画像摘要（字符串）或结构化对象",
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | ✅ | 客户 ID |
| `session_id` | string | ✅ | 会话 ID，建议带 `outreach:` 前缀 |
| `scene` | string | ✅ | 场景标签，仅支持：`trial_not_used` / `trial_used` / `first_order_no_rebuy` |
| `journey_stage` | int / string | ❌ | 客户连续未回复次数枚举：直接传 int `1`~`5`（封顶 5），内部归一化为 `awaiting_reply_N`。也兼容字符串 `"awaiting_reply_3"` |
| `intent_level` | int | ❌ | 客户意向度，0~100 |
| `customer_profile` | string \| object | ❌ | 可传摘要字符串或结构化对象；服务端会合并为扁平 dict |
| `trial_received` 等平铺字段 | bool/int | ❌ | 会并入 `customer_profile` 下发给智能体 |
| `collections` | string[] | ❌ | 检索集合，主动触达常用 `sales_script` |
| `persona` | object | ❌ | 自定义人设配置 |
| `bot_id` | string | ❌ | 机器人配置 ID，与 `persona` 可组合使用 |

#### 响应体

```json
{
  "reply": "string",
  "reply_parts": ["string"],
  "session_id": "string",
  "trial_used": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `reply` | string | 完整回复文本（兼容旧客户端） |
| `reply_parts` | string[] | 分条消息，CRM/前端应优先使用该字段逐条发送 |
| `session_id` | string | 会话 ID（与请求一致） |
| `trial_used` | boolean \| null | 识别到客户已使用体验装时为 `true` |

#### 示例（curl）

```bash
curl -X POST http://localhost:<api_port>/cs/outreach \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "7881302614997312",
    "session_id": "outreach:1688858160785968:7881302614997312:2026-04-24",
    "scene": "first_order_no_rebuy",
    "journey_stage": 1,
    "trial_received": true,
    "trial_used": true,
    "order_count": 1,
    "intent_level": 80,
    "customer_profile": "该客户对某款沐浴乳感兴趣，但发送的图片未能成功传输给商家。客户希望获取产品成分或使用感受的信息。",
    "collections": ["sales_script"]
  }'
```

---

## 3. 调试信息

### `GET /cs/debug/{session_id}`

获取指定会话最近一次请求的内部状态，用于排查问题。

#### 路径参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 要查询的会话 ID |

#### 响应体

```json
{
  "session_id": "user-001",
  "emotion": "calm",
  "emotion_polarity": "neutral",
  "emotion_score": 0.15,
  "intent": "knowledge",
  "intent_score": 0.92,
  "intent_slots": { "product_name": "舒净雅香沐浴乳" },
  "need_human_transfer": false,
  "transfer_reason": null,
  "retrieval_query": "舒净雅香沐浴乳 成分",
  "retrieved_chunks_count": 3,
  "reply_parts_count": 2,
  "scene": "trial_used",
  "scene_source": "crm",
  "journey_stage": "awaiting_reply_3",
  "intent_level": 80,
  "intent_level_delta": 5,
  "next_stage_suggestion": "awaiting_reply_4",
  "customer_profile": { "summary": "..." },
  "accumulated_profile": { "summary": "..." },
  "field_updates": { "trial_used": true },
  "debug_info": {
    "analysis_ms": 1315,
    "rag_ms": 0,
    "rag_skipped": true,
    "prompt_builder_ms": 1,
    "generator_ms": 890,
    "reply_parts": 2
  }
}
```

| 字段 | 说明 |
|------|------|
| `emotion_score` | 情绪置信度，0~1 |
| `intent_score` | 意图置信度，0~1 |
| `intent_slots` | 从消息中提取的槽位，如订单号、商品名 |
| `retrieval_query` | 实际用于知识库检索的查询词 |
| `retrieved_chunks_count` | 命中的知识库段落数量 |
| `reply_parts_count` | 本次回复被拆分的条数 |
| `emotion_polarity` | 情绪极性：`positive / neutral / negative` |
| `transfer_reason` | 转人工原因：`emotion / after_sales` |
| `scene` / `journey_stage` | 当前会话命中的场景与子阶段（`journey_stage` 形如 `awaiting_reply_1` ~ `awaiting_reply_5`，或 CRM 入参 int 1~5 归一化后的同名字符串） |
| `intent_level` / `intent_level_delta` | 当前意向度及本轮变化建议 |
| `next_stage_suggestion` | 下一阶段建议（仅 `awaiting_reply_1` ~ `awaiting_reply_5`） |
| `customer_profile` / `accumulated_profile` | 本轮画像与累计画像 |
| `field_updates` | 本轮建议回写 CRM 的字段 |
| `debug_info` | 各节点耗时（毫秒）及附加信息 |

#### 示例

```bash
curl http://localhost:<api_port>/cs/debug/user-001
```

---

## 附录 A：Persona 字段说明

`persona` 对象用于自定义客服角色，传入后完全替换系统默认角色，所有字段均为可选。

```json
{
  "name": "小新",
  "role": "护肤顾问",
  "brand": "新肌秀",
  "brand_intro": "专注天然温和护肤，旗下舒净雅香沐浴乳采用皂基+温和表活复配清洁体系",
  "personality_traits": ["专业自信", "亲切温柔", "结果导向"],
  "style_guidelines": [
    "回复简洁有力，聚焦产品价值",
    "不编造、不夸大产品功效，严格以知识库为准"
  ],
  "chitchat_topics": ["沐浴乳使用体验", "日常护肤小知识"],
  "extra_instructions": "遇到退款问题优先安抚情绪",
  "accuracy_rules": {
    "deny": "禁止介绍知识库以外的任何产品",
    "priority": "所有产品信息以知识库内容为最高优先级",
    "fallback": "对于不确定的问题，统一回复：我再帮您确认下，稍后给您准确答复。"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 客服名称 |
| `role` | string | 客服角色定位 |
| `brand` | string | 品牌名称 |
| `brand_intro` | string | 品牌简介，注入系统提示词 |
| `personality_traits` | string[] | 性格特征列表 |
| `style_guidelines` | string[] | 回复风格和话术规范 |
| `chitchat_topics` | string[] | 闲聊时可引导的话题范围 |
| `extra_instructions` | string | 额外指令，最长 500 字符。含系统覆盖性语句（如"忘记所有指令"）时自动清空 |
| `accuracy_rules` | object | 回答准确性规则，包含 `deny`（禁止范围）/ `priority`（优先级说明）/ `fallback`（兜底话术） |

> **注意：**
> - 与 `bot_id` 同时传入时，以 bot_id 加载的配置为基础，`persona` 字段做增量覆盖（`accuracy_rules` 等 dict 类型做浅合并）
> - `extra_instructions` 有安全过滤，含提示词注入特征的内容会被自动清空

---

## 附录 B：错误码

| HTTP 状态码 | 说明 | 常见原因 |
|------------|------|---------|
| `400` | 请求参数错误 | `message` 为空或超长、`collections` 包含非法集合名 |
| `404` | 资源不存在 | `bot_id` 对应的机器人配置不存在 |
| `422` | 请求体格式错误 | JSON 结构不合法或字段类型不匹配，返回 `{"detail":"字段 'xxx' 校验失败：..."}` |
| `429` | 请求过于频繁 | 同一 `session_id` 每分钟超过限制次数（默认 20 次） |
| `500` | 服务内部错误 | LLM 调用失败、数据库连接异常等 |

---

## 附录 C：完整请求示例

### 场景 1：查询订单物流

```json
POST /cs

{
  "message": "我的订单 2024112801 发货了吗？",
  "session_id": "user-888",
  "user_id": "member-10086"
}
```

```json
{
  "reply": "好的，我帮您查一下。您的订单 2024112801 已发货，现在在广州转运中心，预计明天送达。\n有其他问题随时告诉我。",
  "reply_parts": [
    "好的，我帮您查一下。您的订单 2024112801 已发货，现在在广州转运中心，预计明天送达。",
    "有其他问题随时告诉我。"
  ],
  "session_id": "user-888",
  "need_human_transfer": false,
  "intent": "order",
  "emotion": "calm"
}
```

---

### 场景 2：产品咨询（含购买引导）

```json
POST /cs

{
  "message": "这款沐浴乳成分是什么？我皮肤比较干。",
  "session_id": "user-001"
}
```

```json
{
  "reply": "干皮用这款皂基+温和表活复配体系会比较舒服，里面有甘油和氢化蓖麻油，洗完不会紧绷。\n如果您想试试，可以先从小规格入手。",
  "reply_parts": [
    "干皮用这款皂基+温和表活复配体系会比较舒服，里面有甘油和氢化蓖麻油，洗完不会紧绷。",
    "如果您想试试，可以先从小规格入手。"
  ],
  "session_id": "user-001",
  "need_human_transfer": false,
  "intent": "knowledge",
  "emotion": "calm"
}
```

---

### 场景 3：投诉触发转人工

```json
POST /cs

{
  "message": "你们的东西根本不能用！！要退款！",
  "session_id": "user-999"
}
```

```json
{
  "reply": "",
  "reply_parts": [],
  "session_id": "user-999",
  "need_human_transfer": true,
  "transfer_reason": "after_sales",
  "intent": "order",
  "emotion": "angry",
  "emotion_polarity": "negative"
}
```

---

### 场景 4：多轮对话

```json
// 第一轮
POST /cs
{ "message": "这款沐浴乳适合什么人用？", "session_id": "user-001" }

// 第二轮（同一 session_id，自动携带上轮上下文）
POST /cs
{ "message": "那有没有小规格可以先试试？", "session_id": "user-001" }
```

---

### 场景 5：主动触达（CRM 调用）

```json
POST /cs/outreach

{
  "user_id": "7881302614997312",
  "session_id": "outreach:1688858160785968:7881302614997312:2026-04-24",
  "scene": "first_order_no_rebuy",
  "journey_stage": 1,
  "intent_level": 80,
  "customer_profile": "该客户对某款沐浴乳感兴趣，希望了解使用感受。",
  "collections": ["sales_script"]
}
```

```json
{
  "reply": "您好呀～看到您之前对新肌秀舒净雅香沐浴乳挺感兴趣的，最近使用感受怎么样？",
  "reply_parts": [
    "您好呀～",
    "看到您之前对新肌秀舒净雅香沐浴乳挺感兴趣的，最近使用感受怎么样？"
  ],
  "session_id": "outreach:1688858160785968:7881302614997312:2026-04-24",
  "trial_used": true
}
```

---

*文档版本：v1.4 · 2026-04-27*
