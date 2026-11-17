"""
电商客服智能体 FastAPI 路由

端点列表：
  POST /cs          普通对话，返回完整回复及分条消息列表
  GET  /cs/debug/{session_id}   查看会话调试信息
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.dependencies import get_cs_agent as _get_cs_agent

logger = logging.getLogger(__name__)

# ── 常量 ───────────────────────────────────────────────────────
# 允许的知识库集合名，新增集合时同步维护此集合
_VALID_COLLECTIONS = {
    "products", "faq", "sop", "policy",
    "agent_memory", "moments_knowledge", "cs_knowledge", "sales_script",
    "palm_diagnosis",
}

_VALID_SCENES = {
    "trial_not_used", "trial_used", "first_order_no_rebuy",
}


# ── 限流工具函数（Redis 分布式版）──────────────────────────────────
async def _check_session_limit(session_id: str) -> bool:
    """
    检查 session 是否超过每分钟对话频率限制（Redis 分布式）。
    返回 True 表示允许本次请求，False 表示触发限流。
    """
    from agent_framework.config.settings import get_settings
    s = get_settings()
    if not s.rate_limit_enabled:
        return True
    from api.rate_limiter import get_rate_limiter
    limiter = await get_rate_limiter()
    return await limiter.is_allowed("session", session_id, s.rate_limit_session_per_minute)


# ── 路由实例 ────────────────────────────────────────────────────
router = APIRouter(prefix="/cs", tags=["电商客服"])


# ── 请求模型 ────────────────────────────────────────────────────
class CSChatRequest(BaseModel):
    message: str = Field(
        ...,
        description="用户消息内容",
        min_length=1,
        max_length=2048,
    )
    session_id: str = Field(
        default="default",
        description="会话 ID，用于多轮对话记忆隔离，同一用户应保持一致",
        max_length=128,
    )
    user_id: Optional[str] = Field(
        default=None,
        description="用户 ID，查询订单/物流时使用",
        max_length=128,
    )
    bot_id: Optional[str] = Field(
        default=None,
        description=(
            "机器人配置 ID。传入后自动从 persona_store 加载对应的角色和知识库配置，"
            "优先级高于 persona/collections 字段。未传入时使用系统默认角色。"
        ),
    )
    brand: Optional[str] = Field(
        default=None,
        description=(
            "品牌名。传入后自动匹配对应 bot_id（如「美城生活」→ mcity_palm_diagnosis），"
            "并加载该 bot 的角色、知识库、业务规则。与 bot_id 同时传入时 bot_id 优先。"
        ),
    )
    collections: Optional[list[str]] = Field(
        default=None,
        description=(
            "知识库集合列表，支持多库并发检索，如 ['products', 'faq']。"
            "未传入时使用 bot_id 配置或默认集合 ['products']。"
        ),
    )
    persona: Optional[dict] = Field(
        default=None,
        description=(
            "自定义角色配置字典（可选）。传入后完全替换默认角色，用于 toC 多品牌场景。"
            "与 bot_id 同时传入时，以 bot_id 为基础，persona 字段做增量覆盖。"
            "支持字段：name / role / brand / brand_intro / personality_traits / "
            "style_guidelines / chitchat_topics / extra_instructions / accuracy_rules"
        ),
    )
    human_handled: bool = Field(
        default=False,
        description=(
            "是否为人工处理完毕后转回 AI 的首条消息。"
            "为 True 时，AI 将忽略历史记录中的激动情绪，避免重复触发转人工。"
        ),
    )

    # ── 客户生命周期字段（CRM 状态机传入；未传时本地推断）──────────────
    scene: Optional[str] = Field(
        default=None,
        description=(
            "客户生命周期场景标签。CRM 状态机维护并传入；未传时由 analysis_node 根据 customer_profile 推断。"
            "可选值：trial_not_used / trial_used / first_order_no_rebuy"
        ),
    )
    journey_stage: Optional[Union[int, str]] = Field(
        default=None,
        description=(
            "客户子阶段。CRM 直接传整数枚举 1~5 表示连续未回复次数（封顶 5），"
            "内部归一化为 awaiting_reply_N；也兼容直接传字符串 awaiting_reply_3 等。"
        ),
    )
    intent_level: Optional[int] = Field(
        default=None,
        description="客户意向度 0-100，用于 prompt 调整推进力度。",
        ge=0,
        le=100,
    )
    customer_profile: Optional[dict] = Field(
        default=None,
        description=(
            "客户档案（扁平 dict）。常用字段："
            "trial_received / trial_used / trial_received_at / "
            "last_order_at / days_since_last_order / order_count / "
            "last_reply_at / do_not_disturb"
        ),
    )

    @field_validator("scene")
    @classmethod
    def validate_scene(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v in _VALID_SCENES:
            return v
        raise ValueError(f"不支持的 scene: {v}，可用值: {sorted(_VALID_SCENES)}")


def _validate_collections(collections: Optional[list[str]]) -> None:
    """校验 collections（在 _resolve_bot_config 之后调用，确保 bot 配置的集合也被校验）。"""
    if collections is None:
        return
    invalid = [c for c in collections if c not in _VALID_COLLECTIONS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的知识库集合: {invalid}，可用集合: {sorted(_VALID_COLLECTIONS)}",
        )


# ── 响应模型 ────────────────────────────────────────────────────
class CSChatResponse(BaseModel):
    reply: str = Field(description="完整回复文本（用于兼容旧客户端）")
    reply_parts: list[str] = Field(
        default=[],
        description=(
            "分条消息列表。模拟人类客服分多条发送的效果，"
            "前端可按顺序逐条渲染，每条间隔 500~800ms。"
            "若回复无需分条则列表仅含一项。"
        ),
    )
    session_id: str = Field(description="当前会话 ID")
    need_human_transfer: bool = Field(
        default=False,
        description="是否需要转人工客服（情绪激动或售后类触发）",
    )
    transfer_reason: Optional[str] = Field(
        default=None,
        description="转人工原因：emotion=情绪激动话术；after_sales=退货退款投诉类，reply 为空由 CRM 发话",
    )
    intent: str = Field(default="", description="识别到的用户意图")
    intents: list[str] = Field(default_factory=list, description="识别到的意图列表（多意图如 order+knowledge）")
    emotion: str = Field(default="", description="识别到的用户情绪")
    emotion_polarity: str = Field(default="neutral", description="情绪极性：positive/negative/neutral")

    # ── 场景化字段（CRM 消费）──────────────────────────────────
    trial_used: Optional[bool] = Field(default=None, description="检测到客户已使用体验装时为 True，CRM 据此切换场景")


# ── 工具函数：加载并合并 bot 配置 ─────────────────────────────────
def _resolve_bot_config(request: CSChatRequest) -> CSChatRequest:
    """
    若请求携带 bot_id，从 persona_store 加载配置并与请求字段合并。
    若只有 brand 无 bot_id，自动按品牌名匹配对应 bot。
    合并规则：bot_id 配置为基础，request.persona 字段做增量覆盖（dict 类型浅合并）。
    """
    # brand → bot_id 自动匹配
    if not request.bot_id and request.brand:
        from api.persona_store import find_by_brand
        matched = find_by_brand(request.brand)
        if matched:
            request.bot_id = matched
        else:
            raise HTTPException(
                status_code=400,
                detail=f"未找到品牌「{request.brand}」对应的 Bot 配置，请先在管理页面创建",
            )

    if not request.bot_id:
        return request

    from api.persona_store import get_persona
    bot_cfg = get_persona(request.bot_id)
    if bot_cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Bot '{request.bot_id}' 不存在，请先在管理页面创建",
        )

    # collections：优先使用请求中传入的，否则用 bot 配置的，最后兜底 products
    if request.collections is None:
        request.collections = bot_cfg.get("collections") or ["products"]

    # persona：以 bot_cfg 为基础，request.persona 增量覆盖
    if request.persona is None:
        request.persona = bot_cfg
    else:
        merged = dict(bot_cfg)
        for k, v in request.persona.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        request.persona = merged

    return request


# ── 路由：普通对话 ──────────────────────────────────────────────
@router.post(
    "",
    response_model=CSChatResponse,
    summary="客服普通对话",
    description=(
        "发送用户消息，经过情绪识别→意图识别→工具/RAG→生成回复完整流程，"
        "返回回复文本及分条消息列表。"
    ),
)
async def cs_chat(request: CSChatRequest) -> CSChatResponse:
    # Session 级限流
    if not await _check_session_limit(request.session_id):
        from agent_framework.config.settings import get_settings
        limit = get_settings().rate_limit_session_per_minute
        raise HTTPException(
            status_code=429,
            detail=f"对话过于频繁，每分钟最多 {limit} 次，请稍后再试",
        )

    try:
        request = _resolve_bot_config(request)
        _validate_collections(request.collections)
        agent = await _get_cs_agent()
        result = await agent.achat(
            message=request.message,
            session_id=request.session_id,
            user_id=request.user_id,
            persona=request.persona,
            collection=request.collections,
            human_handled=request.human_handled,
            scene=request.scene,
            journey_stage=request.journey_stage,
            intent_level=request.intent_level,
            customer_profile=request.customer_profile,
        )
        reply = result.get("reply") or ""
        rp = result.get("reply_parts")
        if isinstance(rp, list):
            reply_parts = rp
        elif reply:
            reply_parts = [reply]
        else:
            reply_parts = []
        return CSChatResponse(
            reply=reply,
            reply_parts=reply_parts,
            session_id=request.session_id,
            need_human_transfer=result["need_human_transfer"],
            transfer_reason=result.get("transfer_reason"),
            intent=result["intent"],
            intents=list(result.get("intents") or ([result["intent"]] if result.get("intent") else [])),
            emotion=result["emotion"],
            emotion_polarity=result.get("emotion_polarity", "neutral"),
            trial_used=(result.get("field_updates") or {}).get("trial_used"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[cs_chat] 处理失败", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 路由：主动触达（CRM 调用，AI 先发消息）──────────────────────
class CSOutreachRequest(BaseModel):
    """
    与 CRM 状态机对齐：可将客户档案中的布尔/计数与 scene 一并平铺 POST；
    customer_profile 可为自然语言摘要字符串，或与平铺字段合并为 dict 传入下游。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": "7881302614997312",
                    "session_id": "outreach:1688858160785968:7881302614997312:2026-04-24",
                    "scene": "first_order_no_rebuy",
                    "journey_stage": 1,
                    "trial_received": True,
                    "trial_used": True,
                    "order_count": 1,
                    "intent_level": 80,
                    "customer_profile": "该客户对某款沐浴乳感兴趣，但发送的图片未能成功传输给商家。客户希望获取产品成分或使用感受的信息。",
                }
            ]
        }
    )

    user_id: str = Field(..., description="客户 ID（必填）", max_length=128)
    session_id: str = Field(..., description="会话 ID（必填），建议含 outreach 前缀便于排查", max_length=128)
    scene: str = Field(..., description=f"场景标签（必填），可选值：{sorted(_VALID_SCENES)}")
    journey_stage: Optional[Union[int, str]] = Field(default=None, description="子阶段。CRM 主动触达时直接传 1~5 整数枚举（连续未回复次数，封顶 5），亦兼容字符串 awaiting_reply_3 等")
    intent_level: Optional[int] = Field(default=None, ge=0, le=100)
    trial_received: Optional[bool] = Field(default=None, description="是否已领取体验装（与 customer_profile 合并）")
    trial_used: Optional[bool] = Field(default=None, description="是否已使用体验装（与 customer_profile 合并）")
    order_count: Optional[int] = Field(default=None)
    days_since_last_order: Optional[int] = Field(default=None)
    consecutive_no_reply: Optional[int] = Field(default=None)
    in_low_freq_pool: Optional[bool] = Field(default=None)
    customer_profile: Optional[Union[str, dict[str, Any]]] = Field(
        default=None,
        description="客户画像：自然语言摘要字符串，或与 trial_received 等并列的结构化 dict；服务层会合并为扁平 dict",
    )
    persona: Optional[dict] = Field(default=None)
    bot_id: Optional[str] = Field(default=None)
    collections: Optional[list[str]] = Field(
        default=None,
        description="检索集合，主动触达默认可走 bot 配置；显式传入时常用 sales_script",
    )

    @field_validator("scene")
    @classmethod
    def validate_outreach_scene(cls, v: str) -> str:
        if v not in _VALID_SCENES:
            raise ValueError(f"不支持的 scene: {v}，可用值: {sorted(_VALID_SCENES)}")
        return v


class CSOutreachResponse(BaseModel):
    reply: str
    reply_parts: list[str] = []
    session_id: str
    trial_used: Optional[bool] = None


@router.post(
    "/outreach",
    response_model=CSOutreachResponse,
    summary="主动触达（CRM 状态机调用）",
    description=(
        "由 CRM 状态机按客户生命周期调用，AI 主动发出首条消息（不响应客户输入）。"
        "走 outreach 子图：outreach_init → rag(sales_script) → prompt_builder → generator。"
    ),
)
async def cs_outreach(request: CSOutreachRequest) -> CSOutreachResponse:
    if not await _check_session_limit(request.session_id):
        from agent_framework.config.settings import get_settings
        limit = get_settings().rate_limit_session_per_minute
        raise HTTPException(
            status_code=429,
            detail=f"触达过于频繁，每分钟最多 {limit} 次，请稍后再试",
        )

    try:
        # 兼容 bot_id：复用 _resolve_bot_config 解析 persona / collections
        if request.bot_id:
            shim = CSChatRequest(
                message=" ", session_id=request.session_id, user_id=request.user_id,
                bot_id=request.bot_id, collections=request.collections, persona=request.persona,
            )
            shim = _resolve_bot_config(shim)
            request.persona = shim.persona
            request.collections = shim.collections

        # 主动触达必须包含 sales_script，避免 bot 默认 collections 仅 products 导致场景话术无法命中
        if request.collections is None:
            request.collections = ["sales_script"]
        elif "sales_script" not in request.collections:
            request.collections = [*request.collections, "sales_script"]

        _validate_collections(request.collections)

        agent = await _get_cs_agent()

        # 合并平铺字段到 customer_profile dict
        crm_profile: dict = {}
        if isinstance(request.customer_profile, dict):
            crm_profile = request.customer_profile
        elif isinstance(request.customer_profile, str):
            crm_profile = {"summary": request.customer_profile}
        for _f in ("trial_received", "trial_used", "order_count",
                   "days_since_last_order", "consecutive_no_reply", "in_low_freq_pool"):
            _v = getattr(request, _f, None)
            if _v is not None:
                crm_profile[_f] = _v

        result = await agent.achat_outreach(
            user_id=request.user_id,
            session_id=request.session_id,
            scene=request.scene,
            journey_stage=request.journey_stage,
            intent_level=request.intent_level,
            customer_profile=crm_profile or None,
            persona=request.persona,
            collection=request.collections,
        )
        return CSOutreachResponse(
            reply=result.get("reply", ""),
            reply_parts=result.get("reply_parts") or [result.get("reply", "")],
            session_id=request.session_id,
            trial_used=(result.get("field_updates") or {}).get("trial_used"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[cs_outreach] 处理失败", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 路由：调试信息 ──────────────────────────────────────────────
@router.get(
    "/debug/{session_id}",
    summary="获取会话调试信息",
    description="返回指定会话最近一次请求的各节点耗时、意图、情绪、检索结果数量等调试数据。",
)
async def cs_debug(session_id: str) -> dict:
    agent = await _get_cs_agent()
    config = {"configurable": {"thread_id": session_id}}
    try:
        state = await agent._graph.aget_state(config)
        v = state.values
        return {
            "session_id":             session_id,
            "emotion":                v.get("emotion"),
            "emotion_polarity":       v.get("emotion_polarity"),
            "emotion_score":          v.get("emotion_score"),
            "intent":                 v.get("intent"),
            "intent_score":           v.get("intent_score"),
            "intent_slots":           v.get("intent_slots"),
            "need_human_transfer":    v.get("need_human_transfer"),
            "transfer_reason":        v.get("transfer_reason"),
            "retrieval_query":        v.get("retrieval_query"),
            "retrieved_chunks_count": len(v.get("retrieved_chunks") or []),
            "reply_parts_count":      len(v.get("reply_parts") or []),
            "scene":                  v.get("scene"),
            "scene_source":           v.get("scene_source"),
            "journey_stage":          v.get("journey_stage"),
            "intent_level":           v.get("intent_level"),
            "intent_level_delta":     v.get("intent_level_delta"),
            "next_stage_suggestion":  v.get("next_stage_suggestion"),
            "customer_profile":       v.get("customer_profile"),
            "accumulated_profile":    (v.get("accumulated_slots") or {}).get("profile"),
            "field_updates":          v.get("field_updates"),
            "debug_info":             v.get("debug_info"),
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"会话不存在或已过期: {e}")
