# 朋友圈互动接口文档（/moments）

**Base URL：** `http://<host>:8000`
**Content-Type：** `application/json`
**字符编码：** UTF-8

---

## 目录

- [1. 接口概览](#1-接口概览)
- [2. 朋友圈互动](#2-朋友圈互动)
- [附录 A：情绪判断逻辑](#附录-a-情绪判断逻辑)
- [附录 B：评论生成逻辑](#附录-b-评论生成逻辑)
- [附录 C：错误码](#附录-c-错误码)
- [附录 D：完整场景示例](#附录-d-完整场景示例)

---

## 1. 接口概览

`/moments` 用于自动处理朋友圈互动，核心逻辑：

```
接收朋友圈内容
      ↓
  情绪判断
    ↙     ↘
 积极      消极
  ↓          ↓
点赞        不做任何操作
  ↓
检索话术知识库
  ↓
生成评论
```

**一次请求返回两个决策：**
- `like`：是否点赞（布尔值）
- `comment`：评论内容（积极时生成，消极时为 `null`）

---

## 2. 朋友圈互动

### `POST /moments`

分析朋友圈内容的情绪，积极则点赞并生成评论，消极则不做任何操作。

#### 请求体

```json
{
  "content": "string",
  "session_id": "string"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | ✅ | 朋友圈文字内容，1~1000 字符 |
| `session_id` | string | ❌ | 会话 ID，用于日志追踪。默认 `"default"` |

#### 响应体

```json
{
  "like": true,
  "comment": "string | null",
  "session_id": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `like` | boolean | `true` = 点赞；`false` = 不点赞 |
| `comment` | string \| null | 生成的评论内容（仅 `like: true` 时有值，其余为 `null`） |
| `session_id` | string | 与请求一致 |

#### 示例

**积极内容：**

```bash
curl -X POST http://localhost:8000/moments \
  -H "Content-Type: application/json" \
  -d '{
    "content": "新品上市！玫瑰系列面霜终于来了，皮肤喝饱水的感觉！",
    "session_id": "wx-user-001"
  }'
```

```json
{
  "like": true,
  "comment": "终于等到你！玫瑰香超治愈，用完像被温柔抱住～",
  "session_id": "wx-user-001"
}
```

**消极内容：**

```bash
curl -X POST http://localhost:8000/moments \
  -H "Content-Type: application/json" \
  -d '{
    "content": "投诉无门，售后态度太差，再也不买了",
    "session_id": "wx-user-002"
  }'
```

```json
{
  "like": false,
  "comment": null,
  "session_id": "wx-user-002"
}
```

---

## 附录 A：情绪判断逻辑

情绪判断为二分类，判断结果直接决定后续动作：

| 判断结果 | 触发条件 | 后续动作 |
|---------|---------|---------|
| `positive`（积极） | 内容正面、愉快、分享、晒图、喜悦等 | 点赞 + 生成评论 |
| `negative`（消极） | 内容负面、抱怨、悲伤、愤怒、无聊等 | 不做任何操作 |

**判断失败时**（网络超时、模型异常等）保守降级为消极，即不点赞不评论，避免误操作。

**示例对照：**

| 内容 | 判断 |
|------|------|
| 今天天气超好，和朋友去爬山了！ | positive |
| 新品到货，颜值太高了！ | positive |
| 周末逛街买了好多好多～ | positive |
| 加班到深夜，累死了 | negative |
| 心情很差，什么都不想做 | negative |
| 投诉无门，再也不买了 | negative |

---

## 附录 B：评论生成逻辑

评论仅在情绪为积极时生成，生成依据：

1. **话术知识库**（`moments_knowledge` 集合）：从知识库检索最相关的 3 条话术作为参考
2. **朋友圈内容本身**：评论内容会结合原文语义生成

**生成规格：**
- 字数：15~40 字
- 风格：温暖、自然、真诚，不像广告
- 知识库为空时：模型自由发挥，风格保持一致

**上传评论话术：**

通过管理后台（`/admin/`）或入库接口上传话术文档，指定集合为 `moments_knowledge`，即可让评论更贴合品牌风格。

```bash
# 通过接口上传话术文件
curl -X POST http://localhost:8000/ingest/upload \
  -F "file=@话术库.txt" \
  -F "collection=moments_knowledge" \
  -F "doc_type=sop"
```

---

## 附录 C：错误码

| HTTP 状态码 | 说明 | 常见原因 |
|------------|------|---------|
| `400` | 请求参数错误 | `content` 为空或超过 1000 字符 |
| `422` | 请求体格式错误 | JSON 结构不合法 |
| `500` | 服务内部错误 | LLM 调用失败（此时保守返回 `like: false, comment: null`） |

> **注意：** 即使 LLM 调用出现异常，接口设计上会尽量返回 200 而非 500，保守降级为不点赞不评论，避免上游系统处理异常。

---

## 附录 D：完整场景示例

### 场景 1：品牌官号发布新品

```json
POST /moments

{
  "content": "全新「玫瑰水光精华」今日正式发售！添加了天然玫瑰萃取，保湿效果提升 30%，限时买一送一～",
  "session_id": "brand-post-20240301"
}
```

```json
{
  "like": true,
  "comment": "玫瑰精华终于来啦！保湿效果这么厉害，囤起来！",
  "session_id": "brand-post-20240301"
}
```

---

### 场景 2：用户晒单

```json
POST /moments

{
  "content": "收到了！包装好精致，玫瑰的味道好香，皮肤用了感觉水润润的，回购！",
  "session_id": "user-wx-abc123"
}
```

```json
{
  "like": true,
  "comment": "谢谢亲的喜欢，回购是对我们最大的认可～",
  "session_id": "user-wx-abc123"
}
```

---

### 场景 3：日常生活分享（非品牌相关）

```json
POST /moments

{
  "content": "今天和闺蜜去吃了火锅，好满足！",
  "session_id": "user-wx-def456"
}
```

```json
{
  "like": true,
  "comment": "和闺蜜的时光最治愈啦，好幸福～",
  "session_id": "user-wx-def456"
}
```

---

### 场景 4：负面内容（不操作）

```json
POST /moments

{
  "content": "最近压力好大，每天都好累，感觉撑不下去了",
  "session_id": "user-wx-ghi789"
}
```

```json
{
  "like": false,
  "comment": null,
  "session_id": "user-wx-ghi789"
}
```

---

### 场景 5：批量处理建议（伪代码）

```python
import httpx

moments = [
    {"content": "今天用了新面霜，皮肤超嫩！", "session_id": "post-001"},
    {"content": "工作太累了，想辞职", "session_id": "post-002"},
    {"content": "周末爬山好开心！", "session_id": "post-003"},
]

async with httpx.AsyncClient() as client:
    for moment in moments:
        resp = await client.post("http://localhost:8000/moments", json=moment)
        result = resp.json()
        if result["like"]:
            # 执行点赞操作
            do_like(moment["session_id"])
        if result["comment"]:
            # 发送评论
            do_comment(moment["session_id"], result["comment"])
```

---

*文档版本：v1.0 · 2026-03-30*
