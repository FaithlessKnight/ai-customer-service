"""
知识库检索节点（异步版，支持多 collection 并发检索）

根据 state.knowledge_collections 动态选择一个或多个知识库集合，
并发检索后按 RRF 分数融合去重，返回综合最优结果。
检索器按 collection 名称缓存，首次初始化后复用，不重复构造。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from functools import lru_cache
from typing import Any

from agents.customer_service.state import CustomerServiceState

logger = logging.getLogger(__name__)

_DEFAULT_COLLECTIONS = ["products"]


@lru_cache(maxsize=16)
def _get_retriever(collection: str):
    """
    按 collection 名称懒加载检索器，结果缓存（最多 16 个不同 collection）。
    """
    try:
        from agent_framework.memory.hybrid_retriever import HybridRetriever
        from agent_framework.memory.vector_store import MilvusRetriever
        from agent_framework.memory.bm25_retriever import BM25Retriever
        retriever = HybridRetriever(
            vector_retriever=MilvusRetriever(collection_name=collection),
            bm25_retriever=BM25Retriever(source="postgresql", collection=collection),
        )
        logger.info("[rag] 检索器初始化完成: collection=%s", collection)
        return retriever
    except Exception as e:
        logger.error("[rag] 检索器初始化失败: collection=%s err=%s", collection, e)
        raise


# awaiting_reply_N → 话术库「第N轮」语义关键词，用于增强向量召回
_AWAITING_ROUND_HINTS = {
    1: "第1轮 提醒使用",
    2: "第2轮 推销活动",
    3: "第3轮 介绍产品功能",
    4: "第4轮 性价比对比",
    5: "第5轮 低压退出",
}

_AGE_SENSITIVE_RE = re.compile(
    r"(?:儿童|小孩|小朋友|婴儿|婴幼儿|孕妇|敏感肌|敏感性皮肤|适用人群|全年龄段|能不能用|可以用吗|是否适合)",
    re.IGNORECASE,
)
_AGE_SENSITIVE_HINT = "适用人群 6岁以上 儿童 婴幼儿 孕妇 咨询医生"


def _parse_awaiting_round(journey_stage: Any) -> int | None:
    """从 awaiting_reply_N / 整数枚举 1~5 解析未回复次数 N，其他值返回 None。"""
    if journey_stage is None or isinstance(journey_stage, bool):
        return None
    if isinstance(journey_stage, int):
        return journey_stage if 1 <= journey_stage <= 5 else None
    if isinstance(journey_stage, str):
        s = journey_stage.strip()
        if not s:
            return None
        if s.isdigit():
            n = int(s)
            return n if 1 <= n <= 5 else None
        if s.startswith("awaiting_reply_"):
            suffix = s.rsplit("_", 1)[-1]
            if suffix.isdigit():
                return max(1, min(5, int(suffix)))
    return None


def _build_query(
    user_input: str,
    slots: dict[str, Any],
    scene: str | None = None,
    journey_stage: Any = None,
) -> str:
    """构建检索查询词，融合槽位信息提升检索精度。

    主动触达模式（user_input 形如 [OUTREACH:scene]）下，
    用 SCENE_LABELS 中的描述词作为 query，避免占位符进入向量空间。
    awaiting_reply_N 状态下追加「第N轮」关键词，让向量召回偏向对应轮次话术。
    """
    round_hint = _AWAITING_ROUND_HINTS.get(_parse_awaiting_round(journey_stage) or 0)

    if user_input.startswith("[OUTREACH"):
        try:
            from agents.customer_service.prompts import SCENE_LABELS
            label, desc = SCENE_LABELS.get(scene or "", ("", ""))
            base = f"{label} {desc}" if desc else (scene or "主动触达 客户场景")
        except Exception:
            base = scene or "主动触达 客户场景"
        return f"{base} {round_hint}" if round_hint else base

    query_parts = [user_input]
    if slots.get("product_name"):
        query_parts.insert(0, slots["product_name"])
    # 年龄段/特殊人群（儿童、孕妇、敏感肌）问题：追加知识库锚点词，提升命中“适用人群”段落概率
    if _AGE_SENSITIVE_RE.search(user_input or ""):
        query_parts.append(_AGE_SENSITIVE_HINT)
    if round_hint:
        query_parts.append(round_hint)
    return " ".join(query_parts)


def _rrf_merge(all_results: list[list[Any]], k: int = 60, top_n: int = 8) -> list[dict[str, Any]]:
    """
    Reciprocal Rank Fusion：将多个检索结果列表融合为单一排序列表。

    Args:
        all_results: 每个 collection 的检索结果列表
        k: RRF 常数（默认 60）
        top_n: 最终返回条数

    Returns:
        融合后按 RRF 分数降序排列的 chunk 列表
    """
    scores: dict[str, float] = {}
    chunks_map: dict[str, dict[str, Any]] = {}

    for results in all_results:
        for rank, r in enumerate(results):
            if r.score <= 0:
                continue
            key = r.content[:120]  # 用内容前缀做去重 key
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in chunks_map:
                chunks_map[key] = {
                    "content":  r.content,
                    "score":    round(r.score, 4),
                    "metadata": r.metadata,
                }

    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [chunks_map[key] for key, _ in merged]


async def _retrieve_one(collection: str, query: str, filters: dict[str, Any] | None = None) -> list[Any]:
    """对单个 collection 执行检索，失败时返回空列表。"""
    try:
        retriever = _get_retriever(collection)
        results = await asyncio.to_thread(retriever.retrieve, query, 5, filters)
        logger.info("[rag] collection=%s hits=%d filters=%s", collection, len(results), filters)
        return results
    except Exception as e:
        logger.error("[rag] 检索失败: collection=%s err=%s", collection, e, exc_info=True)
        return []


async def rag_node(state: CustomerServiceState) -> dict[str, Any]:
    """
    知识库检索节点（异步，支持多 collection 并发检索）。

    若 analysis_node 已并行预取了结果（retrieved_chunks 非空），
    则直接复用，跳过重复检索，节省约 500ms~1s。

    根据 state.knowledge_collections 并发检索所有指定知识库，
    通过 RRF 融合去重后返回综合最优结果。

    示例：
        knowledge_collections=["cs_knowledge", "faq_base"] 会同时检索两个库
        未传入时使用默认 agent_memory

    输入:  user_input / intent_slots / knowledge_collections / retrieved_chunks（可选，预取）
    输出:  retrieved_chunks / retrieval_query
    """
    t0 = time.time()

    # ── 复用 analysis_node 预取结果（如有）───────────────
    prefetched = state.get("retrieved_chunks")
    if prefetched:
        logger.info(
            "[rag] 复用 analysis_node 预取结果，跳过重新检索 (chunks=%d, elapsed=%dms)",
            len(prefetched), round((time.time() - t0) * 1000),
        )
        debug_info = dict(state.get("debug_info") or {})
        debug_info["rag_ms"]      = round((time.time() - t0) * 1000)
        debug_info["rag_skipped"] = True
        return {
            "retrieved_chunks": prefetched,
            "retrieval_query":  state.get("retrieval_query") or state.get("user_input", ""),
            "debug_info":       debug_info,
        }

    user_input  = state.get("user_input", "")
    slots       = state.get("intent_slots") or {}
    scene       = state.get("scene")
    persona_cfg = state.get("persona_config") or {}
    collections = state.get("knowledge_collections") or persona_cfg.get("collections")
    if isinstance(collections, str):           # 兼容旧单字符串传参
        collections = [collections]
    if not collections:
        # scene 非空时自动挂载话术库；否则沿用默认 products
        collections = ["sales_script"] if scene else _DEFAULT_COLLECTIONS
    journey_stage = state.get("journey_stage")
    query = _build_query(user_input, slots, scene, journey_stage)

    # 场景过滤仅下推到 sales_script（该库有 scene/journey_stage 元数据）。
    # products 等库若强加 scene 过滤会触发字段不存在错误，导致召回退化。
    def _filters_for_collection(col: str) -> dict[str, Any] | None:
        if col != "sales_script":
            return None
        f: dict[str, Any] = {}
        if scene:
            # 通用模块（scene=any）始终一并召回
            f["scene"] = [scene, "any"]
        if journey_stage and not str(journey_stage).startswith("awaiting_reply") and not _parse_awaiting_round(journey_stage):
            f["journey_stage"] = journey_stage
        return f or None

    # 并发检索所有 collection
    tasks = [_retrieve_one(col, query, _filters_for_collection(col)) for col in collections]
    all_results = await asyncio.gather(*tasks)

    # 多库融合；单库直接过滤
    if len(collections) == 1:
        chunks = [
            {"content": r.content, "score": round(r.score, 4), "metadata": r.metadata}
            for r in all_results[0] if r.score > 0
        ][:8]
    else:
        chunks = _rrf_merge(list(all_results))

    logger.info("[rag] collections=%s query=%r final_hits=%d", collections, query[:50], len(chunks))

    debug_info = dict(state.get("debug_info") or {})
    debug_info["rag_ms"]          = round((time.time() - t0) * 1000)
    debug_info["rag_collections"] = collections
    debug_info["rag_skipped"]     = False

    return {
        "retrieved_chunks": chunks,
        "retrieval_query":  query,
        "debug_info":       debug_info,
    }
