"""
电商客服智能体状态定义

设计原则：
- 保持 TypedDict 扁平结构（LangGraph 兼容性最佳）
- 按业务阶段分组，字段名加前缀约束，便于维护
- 并行节点写入 debug_info 时通过 Annotated reducer 合并

字段分组：
  [会话]    user_input / session_id / user_id
  [情绪]    emotion_* / need_human_transfer
  [意图]    intent_* / intent_slots
  [工具]    logistics_result / aftersale_result / tool_error
  [检索]    retrieved_chunks / retrieval_query
  [Prompt]  system_prompt / context_text
  [生成]    final_reply / reply_parts
  [控制]    error / debug_info
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from typing_extensions import Annotated
from langgraph.graph import MessagesState

# ── 类型别名 ──────────────────────────────────────────────────
EmotionType = Literal["calm", "excited", "angry", "unknown"]
IntentType  = Literal["order", "knowledge", "chitchat", "unknown"]


# ── Reducer：并行节点各自写入 debug_info 时合并而非覆盖 ───────
def _merge_debug(a: dict, b: dict) -> dict:
    """合并两个 debug_info 字典（并行节点各自写入时使用）。"""
    merged = dict(a or {})
    merged.update(b or {})
    return merged


# ── 主状态类 ─────────────────────────────────────────────────
class CustomerServiceState(MessagesState):
    """
    电商客服智能体的完整状态。

    每个字段由唯一的节点负责写入，其余节点只读：

    节点              写入字段
    ──────────────    ──────────────────────────────────────────
    analysis_node   → emotion_* / intent_* / intent_slots / need_human_transfer / transfer_reason / accumulated_slots / scene / scene_source
    tools_node      → logistics_result / aftersale_result / tool_error
    rag_node        → retrieved_chunks / retrieval_query
    prompt_builder  → system_prompt / context_text
    generator_node  → final_reply / reply_parts / phase_summaries / master_summary / last_active_time / intent_level_delta / next_stage_suggestion
    任意节点         → debug_info (自动合并)
    """

    # ── 会话上下文（请求入口写入，全程只读）─────────────────
    user_input:           str               # 用户原始输入
    session_id:           str               # 会话 ID，用于多轮记忆隔离
    user_id:              Optional[str]     # 用户 ID，查订单/物流时使用
    persona_config:       Optional[dict]    # 外部传入的 Persona 配置字典，None 时使用默认 Persona
    knowledge_collections: Optional[list[str]]   # 挂载的知识库集合名列表，支持多库并发检索，None 时使用默认 agent_memory

    # ── 情绪分析（emotion_node 写入）────────────────────────
    emotion:              EmotionType  # 情绪分类
    emotion_polarity:     str          # 情绪极性：positive/negative/neutral（excited 时区分正负向）
    emotion_score:        float        # 置信度 0~1
    emotion_reason:       str          # 模型判断依据（调试用）
    need_human_transfer:  bool         # True → 跳过意图，直接转人工
    transfer_reason:      Optional[str]     # None / "emotion" / "after_sales"（退货退款投诉静默转 CRM）
    human_handled:        Optional[bool]    # True → 本轮由人工处理完毕后转回 AI，analysis_node 应跳过历史情绪判断

    # ── 意图分析（intent_node 写入）─────────────────────────
    intent:        IntentType          # 主意图（向后兼容，取 intents[0]）
    intents:       list[IntentType]    # 多意图列表（支持 order+knowledge 并发），通常含1个值
    intent_score:  float               # 置信度 0~1
    intent_reason: str                 # 模型判断依据（调试用）
    intent_slots:  dict[str, Any]      # 槽位：order_id / tracking_no / product_name 等

    # ── 工具调用结果（tools_node 写入）──────────────────────
    order_result:      Optional[dict]  # 订单/物流查询结果（合并 logistics + aftersale）
    tool_error:        Optional[str]   # 工具调用失败原因

    # ── 知识库检索（rag_node 写入）──────────────────────────
    retrieved_chunks:  list[dict[str, Any]]  # RRF 混合检索结果列表
    retrieval_query:   str                   # 实际检索词（含槽位增强）

    # ── Prompt 构建（prompt_builder_node 写入）──────────────
    system_prompt:  str   # 最终拼接的系统 Prompt
    context_text:   str   # 注入的上下文（检索结果/工具结果/人工提示）
    brand:          str   # 当前角色品牌名（从 persona 解析）
    role:           str   # 当前角色名称（从 persona 解析，兜底"专业客服"）

    # ── 生成结果（generator_node 写入）─────────────────────
    final_reply:          str         # 最终回复文本（完整，用于历史/摘要）
    reply_parts:          list[str]   # 分条消息列表（按 [SPLIT] 拆分，供 API 分条下发）
    conversation_summary: str         # 向后兼容保留，新逻辑使用 phase_summaries/master_summary
    phase_summaries:      list[str]   # 分级摘要：每 ~20 轮生成一个阶段摘要，最多 5 个
    master_summary:       str         # 总摘要：5 个阶段摘要压缩后的全局摘要（300字）
    last_active_time:     Optional[float]  # Unix 时间戳，每轮写入，用于 24h 超时检测

    # ── 跨轮累积槽位（analysis_node 写入，全程累积）────────────
    accumulated_slots:    dict[str, Any]  # 跨轮持久化槽位：order_ids/product_names/issue_types 等

    # ── 客户生命周期（CRM 写入或 analysis_node 推断）────────────
    # 优先级：CRM 显式传入 > customer_profile 推断 > None（向后兼容旧逻辑）
    scene:                Optional[str]       # trial_not_used / trial_used / first_order_no_rebuy / None
    journey_stage:        Optional[Any]       # CRM 传入：int 1~5（连续未回复次数）或字符串 awaiting_reply_N，分析层归一化为后者
    intent_level:         Optional[int]       # 0-100 客户意向度评分
    scene_source:         Optional[str]       # crm / inferred / default，用于追溯字段来源

    # ── 客户档案（CRM 写入；扁平 dict，约定字段：trial_received/trial_used/last_order_at/days_since_last_order/order_count/do_not_disturb 等）
    customer_profile:     Optional[dict[str, Any]]

    # ── AI 输出供 CRM 消费（generator 写入）─────────────────
    intent_level_delta:    Optional[int]      # 本轮意向度变化，CRM 累加到客户档案
    next_stage_suggestion: Optional[str]      # AI 建议的下一阶段
    field_updates:         Optional[dict[str, Any]]  # 其他建议更新的字段

    # ── 语义信号（analysis_node 由 LLM 语义判断写入，generator/链路控制消费）────
    wants_purchase_link:   Optional[bool]     # 本轮是否表达了明确下单/购买/索要购买入口意图

    # ── 流程控制（任意节点可写）─────────────────────────────
    error:      Optional[str]                        # 全局错误信息
    debug_info: Annotated[dict[str, Any], _merge_debug]  # 各节点耗时等调试信息
    _is_outreach: Optional[bool]                     # outreach_init_node 设为 True，prompt_builder 据此注入 OUTREACH_INSTRUCTION
