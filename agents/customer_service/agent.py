"""
电商客服智能体主入口

基于 LangGraph 组装完整的多阶段客服流程图：
情绪识别 → 意图识别 → 工具/RAG/闲聊 → Prompt构建 → 回复生成
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from langgraph.graph import StateGraph, END
from langgraph.types import Send
from langchain_core.messages import HumanMessage

from agents.customer_service.prompts import REPLY_BLANK_USER_MESSAGE
from agents.customer_service.persona import PersonaConfig
from agents.customer_service.state import CustomerServiceState
from agents.customer_service.nodes.analysis import analysis_node
from agents.customer_service.nodes.tools import tools_node
from agents.customer_service.nodes.rag import rag_node
from agents.customer_service.nodes.prompt_builder import prompt_builder_node
from agents.customer_service.nodes.generator import generator_node
from agents.customer_service.nodes.silent_transfer import silent_transfer_node

logger = logging.getLogger(__name__)

def _ensure_windows_selector_event_loop_policy() -> None:
    """Windows 下强制使用 SelectorEventLoopPolicy，兼容 psycopg async。"""
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        current = asyncio.get_event_loop_policy()
        if not isinstance(current, asyncio.WindowsSelectorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logger.info("[checkpointer] 已切换到 WindowsSelectorEventLoopPolicy")


_ensure_windows_selector_event_loop_policy()


# ── 路由函数 ─────────────────────────────────────────────
def route_after_analysis(state: CustomerServiceState) -> list | str:
    """
    分析节点后的路由：
    - 任意 need_human_transfer=True → silent_transfer（空 reply，CRM 发话）
      （原则：转人工由 CRM 统一承接，AI 不主动对客户承诺“正在转接”）
    - order + knowledge 同时 → Send 并行触发 tools + rag
    - 仅 order → tools
    - 仅 knowledge → rag
    - chitchat / unknown → prompt_builder
    """
    if state.get("need_human_transfer"):
        return "silent_transfer"

    intents = state.get("intents") or [state.get("intent", "unknown")]

    # chitchat / unknown → 直接生成
    if not any(i in ("order", "knowledge") for i in intents):
        return "prompt_builder"

    # 多意图：并行执行 tools + rag
    if "order" in intents and "knowledge" in intents:
        return [Send("tools", state), Send("rag", state)]

    # 单意图
    return "tools" if "order" in intents else "rag"


def _build_uncompiled_graph() -> Any:
    """构建未编译的客服图（不含 checkpointer）。"""
    graph = StateGraph(CustomerServiceState)

    graph.add_node("analysis",      analysis_node)
    graph.add_node("tools",         tools_node)
    graph.add_node("rag",           rag_node)
    graph.add_node("prompt_builder", prompt_builder_node)
    graph.add_node("silent_transfer", silent_transfer_node)
    graph.add_node("generator",     generator_node)

    graph.set_entry_point("analysis")

    graph.add_conditional_edges(
        "analysis",
        route_after_analysis,
        {
            "tools":            "tools",
            "rag":                "rag",
            "prompt_builder":     "prompt_builder",
            "silent_transfer":    "silent_transfer",
        },
    )

    graph.add_edge("tools",         "prompt_builder")
    graph.add_edge("rag",           "prompt_builder")
    graph.add_edge("silent_transfer", END)
    graph.add_edge("prompt_builder", "generator")
    graph.add_edge("generator",     END)

    return graph


# ── 主动触达子图 ──────────────────────────────────────────
async def outreach_init_node(state: CustomerServiceState) -> dict[str, Any]:
    """
    主动触达初始化节点：绕过 analysis，固定情绪/意图，预设 user_input 占位供 RAG 构造 query。
    同时把 CRM 直传的 journey_stage（int 1~5 / 字符串）归一化为 awaiting_reply_N，
    保证 outreach 路径回写给 CRM 的字段类型与被动 chat 路径一致。
    """
    from agents.customer_service.nodes.analysis import _resolve_journey_stage

    scene = state.get("scene")
    normalized_stage = _resolve_journey_stage(state, scene)
    return {
        "emotion":             "calm",
        "emotion_polarity":    "neutral",
        "emotion_score":       1.0,
        "emotion_reason":      "主动触达",
        "need_human_transfer": False,
        "transfer_reason":     None,
        "intent":              "knowledge",      # 让 prompt_builder 的 intents 分支落入非 order 路径
        "intents":             ["knowledge"],
        "intent_score":        1.0,
        "intent_reason":       "主动触达",
        "intent_slots":        {},
        "accumulated_slots":   state.get("accumulated_slots") or {},
        "scene_source":        state.get("scene_source") or "crm",
        # 归一化 journey_stage：CRM 传 int 1~5 / 字符串 / awaiting_reply_N → 统一字符串
        "journey_stage":       normalized_stage if normalized_stage is not None else state.get("journey_stage"),
        # 触发 prompt_builder 注入 OUTREACH_INSTRUCTION（不超 2 短句、不发链接、不堆卖点）
        "_is_outreach":        True,
        # user_input 设为场景标签占位，rag._build_query 会识别后替换为 scene 描述词
        "user_input":          f"[OUTREACH:{scene}]" if scene else "[OUTREACH]",
    }


def _build_uncompiled_outreach_graph() -> Any:
    """构建主动触达子图：outreach_init → rag → prompt_builder → generator → END"""
    graph = StateGraph(CustomerServiceState)
    graph.add_node("outreach_init", outreach_init_node)
    graph.add_node("rag",            rag_node)
    graph.add_node("prompt_builder", prompt_builder_node)
    graph.add_node("generator",      generator_node)

    graph.set_entry_point("outreach_init")
    graph.add_edge("outreach_init",  "rag")
    graph.add_edge("rag",            "prompt_builder")
    graph.add_edge("prompt_builder", "generator")
    graph.add_edge("generator",      END)
    return graph


async def build_graph_async() -> Any:
    """异步构建并编译客服智能体图（含 checkpointer）。"""
    checkpointer = await _build_checkpointer_async()
    return _build_uncompiled_graph().compile(checkpointer=checkpointer)


async def build_graphs_async() -> tuple[Any, Any]:
    """
    异步构建并编译主图 + outreach 子图，共享同一 checkpointer。
    Returns: (chat_graph, outreach_graph)
    """
    checkpointer = await _build_checkpointer_async()
    chat_graph = _build_uncompiled_graph().compile(checkpointer=checkpointer)
    outreach_graph = _build_uncompiled_outreach_graph().compile(checkpointer=checkpointer)
    return chat_graph, outreach_graph


async def _build_checkpointer_async():
    """
    构建异步 checkpointer（供 ainvoke/astream 使用）：
    - 优先使用 AsyncRedisSaver（低延迟，高并发友好）
    - Redis 不可用时降级到 AsyncPostgresSaver + 连接池（持久化）
    - PG 也不可用时降级到 MemorySaver（开发/单机模式）
    """
    from agent_framework.config.settings import get_settings
    s = get_settings()

    # ── 优先 Redis ──────────────────────────────────────
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
        redis_url = s.redis_url
        if not redis_url:
            raise ValueError("Redis 未配置")
        checkpointer = AsyncRedisSaver(redis_url=redis_url, ttl={"checkpoint": 86400})
        await checkpointer.asetup()
        logger.info("[checkpointer] 使用 AsyncRedisSaver: %s", redis_url.split("@")[-1] if "@" in redis_url else redis_url)
        return checkpointer
    except Exception as e:
        logger.warning("[checkpointer] Redis 不可用，尝试 PostgreSQL: %s", e)

    # ── 降级 PostgreSQL ─────────────────────────────────
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool
        pg_uri = s.pg_uri
        if not pg_uri:
            raise ValueError("PG 未配置")
        pool = AsyncConnectionPool(
            conninfo=pg_uri,
            min_size=s.pg_pool_min if hasattr(s, 'pg_pool_min') else 5,
            max_size=s.pg_pool_max if hasattr(s, 'pg_pool_max') else 50,
            max_idle=300,
            reconnect_timeout=30,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        logger.info("[checkpointer] 使用 AsyncPostgreSQL 连接池持久化会话: %s", pg_uri.split("@")[-1])
        return checkpointer
    except Exception as e:
        logger.warning(
            "[checkpointer] PostgreSQL 不可用，降级为 MemorySaver（重启后会话历史丢失）: %s", e
        )
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


class CustomerServiceAgent:
    """
    电商客服智能体。

    Example:
        >>> agent = await CustomerServiceAgent.create()
        >>> reply = await agent.achat("这款沐浴露孕妇能用吗", session_id="user-001")
        >>> print(reply)
    """

    def __init__(self, graph: Any, outreach_graph: Optional[Any] = None) -> None:
        self._graph = graph
        self._outreach_graph = outreach_graph
        logger.info("CustomerServiceAgent 初始化完成 (outreach=%s)", bool(outreach_graph))

    @classmethod
    async def create(cls) -> "CustomerServiceAgent":
        """异步工厂方法，构建 agent（含 checkpointer 初始化）。"""
        chat_graph, outreach_graph = await build_graphs_async()
        return cls(chat_graph, outreach_graph)

    def _make_config(self, session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}}

    def _make_input(
        self,
        message: str,
        session_id: str,
        user_id: Optional[str] = None,
        persona: Optional[dict] = None,
        collection: Optional[str] = None,
        human_handled: bool = False,
        scene: Optional[str] = None,
        journey_stage: Optional[Any] = None,
        intent_level: Optional[int] = None,
        customer_profile: Optional[dict] = None,
    ) -> dict:
        return {
            "user_input":           message,
            "session_id":           session_id,
            "user_id":              user_id,
            "persona_config":       persona,
            "knowledge_collections": collection if isinstance(collection, list) else ([collection] if collection else None),
            "messages":             [],
            "intent_slots":         {},
            "accumulated_slots":    {},
            "retrieved_chunks":     [],
            "prefetched_chunks":    [],
            "debug_info":           {},
            "order_result":         None,
            "tool_error":           None,
            "intent":               None,
            "intents":              [],
            "emotion":              None,
            "emotion_polarity":     "neutral",
            "need_human_transfer":  False,
            "transfer_reason":      None,  # emotion | after_sales
            "human_handled":        human_handled,
            "system_prompt":        "",
            "context_text":         "",
            "final_reply":          "",
            "reply_parts":          [],
            # ── 客户生命周期字段 ───────────────────────────
            "scene":                scene,
            "journey_stage":        journey_stage,
            "intent_level":         intent_level,
            "customer_profile":     customer_profile,
            "scene_source":         "crm" if scene else None,
            "intent_level_delta":   None,
            "next_stage_suggestion": None,
            "field_updates":        None,
        }

    def chat(
        self,
        message: str,
        session_id: str = "default",
        user_id: Optional[str] = None,
    ) -> str:
        """同步接口（兼容保留，仅用于非 FastAPI 环境如脚本/测试）。"""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError(
                "chat() 不能在已运行的事件循环中调用，请改用 await achat()"
            )
        return asyncio.run(self.achat(message, session_id, user_id))

    async def achat(
        self,
        message: str,
        session_id: str = "default",
        user_id: Optional[str] = None,
        persona: Optional[dict] = None,
        collection: Optional[str] = None,
        human_handled: bool = False,
        scene: Optional[str] = None,
        journey_stage: Optional[Any] = None,
        intent_level: Optional[int] = None,
        customer_profile: Optional[dict] = None,
    ) -> dict:
        """异步对话接口（FastAPI await 调用），返回完整状态字典。"""
        import asyncio
        if not (message or "").strip():
            logger.info("[agent] 用户消息为空白，跳过图执行 session=%s", session_id)
            # 从 persona 提取 brand/role 用于格式化兜底话术
            p_brand = "客服中心"
            p_role = "专业客服"
            if persona:
                p_cfg = PersonaConfig.from_dict(persona)
                p_brand = p_cfg.brand or p_brand
                p_role = p_cfg.role or p_role
            blank_msg = REPLY_BLANK_USER_MESSAGE.format(brand=p_brand, role=p_role)
            return {
                "reply":               blank_msg,
                "reply_parts":         [blank_msg],
                "intent":              "unknown",
                "emotion":             "calm",
                "emotion_polarity":    "neutral",
                "need_human_transfer": False,
                "transfer_reason":     None,
                "scene":               scene,
                "scene_source":        "crm" if scene else None,
                "intent_level_delta":  None,
                "next_stage_suggestion": None,
                "field_updates":       None,
            }

        state_input = self._make_input(
            message, session_id, user_id, persona, collection, human_handled,
            scene=scene, journey_stage=journey_stage,
            intent_level=intent_level, customer_profile=customer_profile,
        )
        config = self._make_config(session_id)

        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                result = await self._graph.ainvoke(state_input, config=config, durability="exit")
                final_reply = result.get("final_reply", "") or ""
                rp = result.get("reply_parts")
                if isinstance(rp, list):
                    reply_parts = rp
                elif final_reply:
                    reply_parts = [final_reply]
                else:
                    reply_parts = []
                return {
                    "reply":               final_reply,
                    "reply_parts":         reply_parts,
                    "intent":              result.get("intent", ""),
                    "intents":             result.get("intents", []),
                    "emotion":             result.get("emotion", ""),
                    "emotion_polarity":    result.get("emotion_polarity", "neutral"),
                    "need_human_transfer": result.get("need_human_transfer", False),
                    "transfer_reason":     result.get("transfer_reason"),
                    "scene":               result.get("scene"),
                    "scene_source":        result.get("scene_source"),
                    "intent_level_delta":  result.get("intent_level_delta"),
                    "next_stage_suggestion": result.get("next_stage_suggestion"),
                    "field_updates":       result.get("field_updates"),
                }
            except Exception as e:
                err_str = str(e).lower()
                is_pg_error = any(k in err_str for k in ["connection", "closed", "psycopg", "ssl"])
                if attempt == 0 and is_pg_error:
                    logger.warning("[agent] checkpointer 连接异常，重建后重试: %s", e)
                    try:
                        self._graph = await build_graph_async()
                    except Exception as rebuild_e:
                        logger.error("[agent] checkpointer 重建失败: %s", rebuild_e)
                    await asyncio.sleep(0.5)
                else:
                    logger.error("[agent] achat 失败: %s", e, exc_info=True)
                    return {
                        "reply":               "抱歉，系统暂时开小差了，请稍后再试，或联系人工客服为您服务。",
                        "intent":              "",
                        "intents":             [],
                        "emotion":             "",
                        "emotion_polarity":    "neutral",
                        "need_human_transfer": False,
                    }

    async def achat_outreach(
        self,
        user_id: str,
        session_id: str,
        scene: str,
        journey_stage: Optional[Any] = None,
        customer_profile: Optional[dict] = None,
        intent_level: Optional[int] = None,
        persona: Optional[dict] = None,
        collection: Optional[str] = None,
    ) -> dict:
        """
        主动触达：CRM 按状态机定时调用，AI 主动发出首条消息。
        无 user_input；scene 必填。
        """
        if self._outreach_graph is None:
            raise RuntimeError("outreach_graph 未初始化")

        state_input = self._make_input(
            message="",  # 占位，outreach_init_node 会覆盖为 [OUTREACH:scene]
            session_id=session_id,
            user_id=user_id,
            persona=persona,
            collection=collection,
            human_handled=False,
            scene=scene,
            journey_stage=journey_stage,
            intent_level=intent_level,
            customer_profile=customer_profile,
        )
        config = self._make_config(session_id)

        try:
            result = await self._outreach_graph.ainvoke(state_input, config=config, durability="exit")
            return {
                "reply":               result.get("final_reply", ""),
                "reply_parts":         result.get("reply_parts") or [result.get("final_reply", "")],
                "scene":               result.get("scene"),
                "scene_source":        result.get("scene_source"),
                "intent_level_delta":  result.get("intent_level_delta"),
                "next_stage_suggestion": result.get("next_stage_suggestion"),
                "field_updates":       result.get("field_updates"),
            }
        except Exception as e:
            logger.error("[agent] achat_outreach 失败: %s", e, exc_info=True)
            return {
                "reply": "",
                "reply_parts": [],
                "scene": scene,
                "scene_source": "crm",
                "intent_level_delta": None,
                "next_stage_suggestion": None,
                "field_updates": None,
                "error": str(e),
            }



