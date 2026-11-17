"""
健康检查接口路由

提供服务存活检测和各组件状态检查。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from api.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["系统"])

VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health_check() -> HealthResponse:
    """
    检查服务及各组件状态。

    组件状态：
    - ok:          正常
    - unavailable: 未配置或未连接（不影响基本功能）
    - error:       异常
    """
    from agent_framework.config.settings import get_settings
    s = get_settings()
    components: dict[str, str] = {}

    # 检查 LLM 配置
    components["llm"] = "ok" if s.openai_api_key else "unavailable（未配置 OPENAI_API_KEY）"

    # 检查 Milvus
    try:
        from pymilvus import connections
        connections.connect(host=s.milvus_host, port=s.milvus_port, timeout=2)
        connections.disconnect("default")
        components["milvus"] = "ok"
    except Exception as e:
        components["milvus"] = f"unavailable（{e}）"

    # 检查 PostgreSQL
    try:
        from agent_framework.memory.pg_client import PostgresClient
        client = PostgresClient()
        with client.cursor() as cur:
            cur.execute("SELECT 1")
        components["postgresql"] = "ok"
    except Exception as e:
        components["postgresql"] = f"unavailable（{e}）"

    # 检查 BM25 / jieba
    try:
        import jieba  # noqa
        import rank_bm25  # noqa
        components["bm25"] = "ok"
    except ImportError as e:
        components["bm25"] = f"error（{e}）"

    # 检查 Ollama（如果配置了）
    if s.embedding_provider == "ollama":
        try:
            import httpx
            resp = httpx.get(f"{s.ollama_base_url}/api/tags", timeout=2)
            components["ollama"] = "ok" if resp.status_code == 200 else f"error（HTTP {resp.status_code}）"
        except Exception as e:
            components["ollama"] = f"unavailable（{e}）"
    else:
        components["ollama"] = "not configured"

    # 整体状态判断
    has_error = any("error" in v for v in components.values())
    has_unavailable = any("unavailable" in v for v in components.values())

    if has_error:
        overall = "error"
    elif has_unavailable:
        overall = "degraded"
    else:
        overall = "ok"

    return HealthResponse(
        status=overall,
        version=VERSION,
        environment=s.environment,
        components=components,
    )


@router.get("/ping", summary="存活检测")
async def ping() -> dict:
    """简单存活检测，负载均衡器使用。"""
    return {"pong": True}
