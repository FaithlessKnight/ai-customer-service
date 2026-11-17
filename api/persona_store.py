"""
Persona 配置持久化存储

使用 JSON 文件存储，支持多个命名 Persona。
生产环境可替换为 PostgreSQL 存储。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_STORE_PATH = Path("data/personas.json")


def _get_store_path() -> Path:
    from agent_framework.config.settings import get_settings
    s = get_settings()
    base = Path(getattr(s, "data_upload_dir", "./data/uploads")).parent
    return base / "personas.json"


def _normalize(cfg: dict) -> dict:
    """兼容旧字段名，将 knowledge_collection/knowledge_collections 统一为 collections。"""
    if "knowledge_collection" in cfg and "collections" not in cfg:
        old = cfg.pop("knowledge_collection")
        cfg["collections"] = [old] if old else ["products"]
    elif "knowledge_collections" in cfg and "collections" not in cfg:
        cfg["collections"] = cfg.pop("knowledge_collections")
    if "collections" not in cfg:
        cfg["collections"] = ["products"]
    return cfg


def _load_all() -> dict:
    path = _get_store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: _normalize(v) for k, v in data.items()}
    except Exception as e:
        logger.warning("读取 persona 配置失败: %s", e)
        return {}


def _save_all(data: dict) -> None:
    path = _get_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 公开 API ──────────────────────────────────────────────────

def list_personas() -> list[dict]:
    """列出所有已保存的 Persona（含 id 和完整配置）。"""
    data = _load_all()
    return [
        {"id": k, **v}
        for k, v in sorted(data.items(), key=lambda x: x[1].get("updated_at", ""), reverse=True)
    ]


def get_persona(persona_id: str) -> Optional[dict]:
    """根据 ID 获取 Persona，不存在返回 None。"""
    return _load_all().get(persona_id)


def find_by_brand(brand: str) -> Optional[str]:
    """根据品牌名查找对应的 persona_id（bot_id），不存在返回 None。"""
    data = _load_all()
    for pid, cfg in data.items():
        if cfg.get("brand") == brand:
            return pid
    return None


def save_persona(persona_id: str, config: dict) -> dict:
    """保存或更新 Persona。"""
    from datetime import datetime
    data = _load_all()
    config["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data[persona_id] = config
    _save_all(data)
    logger.info("Persona 已保存: id=%s name=%s", persona_id, config.get("name"))
    return {"id": persona_id, **config}


def delete_persona(persona_id: str) -> bool:
    """删除 Persona，返回是否成功。"""
    data = _load_all()
    if persona_id not in data:
        return False
    del data[persona_id]
    _save_all(data)
    logger.info("Persona 已删除: id=%s", persona_id)
    return True


def render_preview(config: dict) -> str:
    """预览 Persona 渲染后的 Prompt 文本。"""
    from agents.customer_service.persona import PersonaConfig
    persona = PersonaConfig.from_dict(config)
    return persona.to_prompt_text()
