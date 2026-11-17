"""
Persona 管理 API 路由

GET    /admin/personas              - 列出所有 Persona
GET    /admin/personas/{id}         - 获取单个 Persona
POST   /admin/personas/{id}         - 创建/更新 Persona
DELETE /admin/personas/{id}         - 删除 Persona
POST   /admin/personas/preview      - 预览 Prompt 渲染结果
GET    /admin/                      - 管理页面入口
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from api.persona_store import (
    list_personas, get_persona, save_persona,
    delete_persona, render_preview,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Persona 管理"])


# ── 请求/响应模型 ──────────────────────────────────────────────
class PersonaPayload(BaseModel):
    name:                str        = Field(...,   description="客服名字")
    role:                str        = Field(...,   description="角色定位")
    brand:               str        = Field(...,   description="品牌名称")
    brand_intro:         str        = Field("",    description="品牌介绍")
    personality_traits:  list[str]  = Field(default_factory=list, description="性格特征列表")
    style_guidelines:    list[str]  = Field(default_factory=list, description="话术规范列表")
    chitchat_topics:     list[str]  = Field(default_factory=list, description="闲聊引导话题列表")
    extra_instructions:  str        = Field("",    description="额外业务指令", max_length=2000)
    collections:         list[str]  = Field(default_factory=lambda: ["products"], description="挂载的知识库集合列表，支持多库并发检索")
    business_rules:      Optional[dict] = Field(default=None, description="结构化业务规则配置（商品命名/物流/链接/示例等）")


class PreviewRequest(BaseModel):
    config: dict[str, Any] = Field(..., description="Persona 配置字典")


# ── 管理页面 ───────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_index():
    """返回可视化管理页面。"""
    import os
    # admin.py 在 api/routers/，static 在 api/static/
    html_path = os.path.join(os.path.dirname(__file__), "..", "static", "admin.html")
    html_path = os.path.normpath(html_path)
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@router.get("/ingest", response_class=HTMLResponse, include_in_schema=False)
async def admin_ingest():
    """返回数据入库可视化管理页面。"""
    import os
    html_path = os.path.join(os.path.dirname(__file__), "..", "static", "ingest.html")
    html_path = os.path.normpath(html_path)
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@router.get("/collections", summary="列出 Milvus 所有 Collection")
async def api_list_collections() -> dict:
    """
    返回系统预定义的四个知识库，同时尝试从 Milvus 补充已有 collection。
    """
    predefined = ["products", "faq", "sop", "policy", "palm_diagnosis"]
    try:
        from pymilvus import MilvusClient
        from agent_framework.config.settings import get_settings
        s = get_settings()
        uri = f"http://{s.milvus_host}:{s.milvus_port}"
        token = f"{s.milvus_user}:{s.milvus_password}" if s.milvus_user and s.milvus_password else ""
        mc = MilvusClient(uri=uri, token=token)
        existing = mc.list_collections()
        # 合并预定义 + 已有，去重排序
        merged = sorted(set(predefined) | set(existing))
        return {"ok": True, "collections": merged}
    except Exception as e:
        logger.warning("[collections] Milvus 查询失败，返回预定义列表: %s", e)
        return {"ok": False, "collections": predefined}


@router.get("/bots", include_in_schema=False)
async def admin_bots():
    """重定向到统一的 Agent 模板管理页。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/")



# ── Persona CRUD ───────────────────────────────────────────────
@router.get("/personas", summary="列出所有 Persona")
async def api_list_personas() -> list[dict]:
    return list_personas()


@router.get("/personas/{persona_id}", summary="获取单个 Persona")
async def api_get_persona(persona_id: str) -> dict:
    p = get_persona(persona_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Persona '{persona_id}' 不存在")
    return {"id": persona_id, **p}


@router.post("/personas/{persona_id}", summary="创建或更新 Persona")
async def api_save_persona(persona_id: str, payload: PersonaPayload) -> dict:
    if not persona_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="persona_id 只能包含字母、数字、连字符和下划线")
    result = save_persona(persona_id, payload.model_dump())
    return {"ok": True, "persona": result}


@router.delete("/personas/{persona_id}", summary="删除 Persona")
async def api_delete_persona(persona_id: str) -> dict:
    ok = delete_persona(persona_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Persona '{persona_id}' 不存在")
    return {"ok": True, "deleted": persona_id}


@router.post("/preview", summary="预览 Prompt 渲染结果")
async def api_preview_persona(req: PreviewRequest) -> dict:
    try:
        text = render_preview(req.config)
        return {"ok": True, "preview": text}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
