# journey_stage 字段规范（精简版）

> 文档用途：定义智能体与 CRM 状态机之间 `journey_stage` 字段的合法值与流转规则。
> 现行约定：仅用 **连续未回复次数** 作为子阶段，其余业务阶段（购买意向 / 异议 / 投诉等）由 `scene` + 当轮意图 + `intent_level` 表达，不再由 `journey_stage` 维护。
> 整理日期：2026-05-07

---

## 字段说明

| 字段名 | 类型 | 传入方向 | 说明 |
|--------|------|----------|------|
| `journey_stage` | int / string | CRM → 智能体 | **建议**直接传 int `1`~`5`（连续未回复次数，封顶 5），服务端归一化为 `awaiting_reply_N`；也兼容字符串 `"awaiting_reply_3"` |
| `next_stage_suggestion` | string | 智能体 → CRM | 本轮对话后建议的下一阶段，固定为 `awaiting_reply_1` ~ `awaiting_reply_5` 之一 |

---

## 合法值

| 值 | 含义 | 智能体行为 |
|----|------|------------|
| `awaiting_reply_1`（CRM 传 `1`） | 客户已 1 次未回复 | 第 1 轮：温和提醒使用，不催促 |
| `awaiting_reply_2`（CRM 传 `2`） | 连续 2 次未回复 | 第 2 轮：顺带说一下买赠活动 |
| `awaiting_reply_3`（CRM 传 `3`） | 连续 3 次未回复 | 第 3 轮：介绍核心产品功能引兴趣 |
| `awaiting_reply_4`（CRM 传 `4`） | 连续 4 次未回复 | 第 4 轮：性价比对比，最后一次正向推进 |
| `awaiting_reply_5`（CRM 传 `5`） | 连续 5 次未回复（封顶） | 第 5 轮：低压退出，礼貌收口、不再追问、不再推产品、不发链接 |

CRM 传入超出 1~5 范围、None 或非法值时，智能体不附加阶段化提示，按场景默认话术处理。

---

## 与 scene 的协同

`scene` 决定走哪一份话术库（`trial_not_used` / `trial_used` / `first_order_no_rebuy`），`journey_stage` 决定该场景下的「第几轮跟进」。

| scene | journey_stage 含义 |
|-------|---------------------|
| `trial_not_used` | 体验装签收后第 N 次未回复跟进 |
| `trial_used` | 体验装已使用后第 N 次未回复跟进 |
| `first_order_no_rebuy` | 首单后关怀的第 N 次未回复跟进 |

到达 `awaiting_reply_5` 后建议 CRM 把客户置入低频维护池，停止主动触达；客户主动回复时按被动客服模式继续对话。

---

## 客户终态（`scene` 字段层面，不在 `journey_stage`）

到达以下状态后，CRM 应停止传入 `scene` 字段，智能体退回普通客服模式：

| 信号 | 含义 | CRM 操作 |
|------|------|----------|
| 客户完成购买 | `customer_profile.order_count` 增加 | 切换 `scene` 或停止场景跟进 |
| `awaiting_reply_5` 已发出仍无回复 | 进入低频维护池 | 降低触达频率，90 天以上再评估 |
| 客户要求免打扰 | `customer_profile.do_not_disturb = true` | 永久停止主动触达 |

---

## CRM 实现要点

1. **每次调用 `/cs` 或 `/cs/outreach` 都要传入当前的 `journey_stage`**（int 1~5 即可），智能体不保存这个状态
2. **收到响应后读取 `next_stage_suggestion`**（形如 `"awaiting_reply_4"`），更新客户档案（取末位数字 +1 也可以）
3. **`intent_level_delta` 要累加**到客户当前的 `intent_level`，而不是替换
4. **客户回复后 `journey_stage` 应重置为 `1`**（计数从 0/1 起算，下一次未回复再递增）
5. **`do_not_disturb = true` 时**，停止所有主动触达，被动对话仍可响应

---

## 用户画像字段规范

### 智能体提取的画像字段（`field_updates.profile`）

每轮对话智能体会从用户消息中提取以下画像信号，通过 `field_updates.profile` 返回：

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `skin_type` | string | 皮肤类型 | `"干性"` / `"油性"` / `"混合"` / `"敏感"` |
| `usage_habit` | string | 使用习惯描述 | `"已试用体验装"` / `"每天洗澡"` |
| `price_sensitive` | bool | 是否价格敏感 | `true` / `false` |
| `purchase_intent` | string | 购买意向强度 | `"high"` / `"medium"` / `"low"` |
| `concern_type` | list | 顾虑类型（可多项） | `["效果疑虑", "价格异议"]` |

> - 字段由 AI 判断，只在有明确证据时填写，不确定则不输出该字段
> - `price_sensitive` 一旦为 `true` 不会被后续覆盖为 `false`
> - `concern_type` 跨轮累积，取历史所有轮次的并集

### CRM 持久化规则

1. **每轮收到 `field_updates.profile` 后存入客户档案**，与已有画像合并（同字段取最新值，`concern_type` 取并集）
2. **下次调用时通过 `customer_profile.profile` 传回**，智能体会在本次会话中以此为初始值继续累积
3. **`field_updates` 为 `null` 时**，本轮无新画像信号，不需要更新档案

### 请求示例（含历史画像回传）

```json
{
  "message": "用户消息",
  "session_id": "xxx",
  "user_id": "xxx",
  "scene": "trial_used",
  "journey_stage": 2,
  "intent_level": 40,
  "customer_profile": {
    "trial_received": true,
    "trial_used": true,
    "order_count": 0,
    "days_since_last_order": null,
    "last_reply_at": "2026-05-01T10:00:00",
    "consecutive_no_reply": 2,
    "do_not_disturb": false,
    "profile": {
      "skin_type": "干性",
      "usage_habit": "已试用体验装",
      "price_sensitive": false,
      "purchase_intent": "medium",
      "concern_type": ["效果疑虑"]
    }
  }
}
```

---

## 历史变更

- 2026-05-07：精简阶段枚举，废除 `first_outreach / positive_feedback / purchase_guided / objection_price / refusal_closing` 等十余个旧阶段名；统一收敛为 `awaiting_reply_1` ~ `awaiting_reply_5`，CRM 可直接传 int。
