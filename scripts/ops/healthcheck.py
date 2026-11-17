#!/usr/bin/env python3
"""
一键巡检：PostgreSQL / Milvus / Redis / Ollama（若配置）等连通性与简要状态。
输出 JSON，便于脚本与告警系统消费。

用法（仓库根）:
    python scripts/ops/healthcheck.py
    python scripts/ops/healthcheck.py --no-pretty
    python scripts/ops/healthcheck.py --api-url http://127.0.0.1:8000   # 额外请求 /health

容器内:
    python scripts/ops/healthcheck.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _check_postgresql() -> dict[str, Any]:
    try:
        from agent_framework.memory.pg_client import get_pg_client

        client = get_pg_client()
        with client.cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok", "detail": None}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_milvus() -> dict[str, Any]:
    try:
        from pymilvus import MilvusClient
        from agent_framework.config.settings import get_settings

        s = get_settings()
        uri = f"http://{s.milvus_host}:{s.milvus_port}"
        token = f"{s.milvus_user}:{s.milvus_password}" if s.milvus_user and s.milvus_password else ""
        mc = MilvusClient(uri=uri, token=token)
        cols = mc.list_collections()
        return {"status": "ok", "detail": None, "collections": cols, "collections_count": len(cols)}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_redis() -> dict[str, Any]:
    try:
        from agent_framework.config.settings import get_settings

        url = get_settings().redis_url
        import redis

        r = redis.from_url(url, socket_connect_timeout=3)
        if r.ping():
            return {"status": "ok", "detail": None, "url_host": url.split("@")[-1][:80]}
        return {"status": "error", "detail": "PING failed"}
    except ImportError:
        return {"status": "skipped", "detail": "redis 包未安装"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_ollama() -> dict[str, Any]:
    try:
        from agent_framework.config.settings import get_settings

        s = get_settings()
        if s.embedding_provider != "ollama":
            return {"status": "skipped", "detail": f"EMBEDDING_PROVIDER={s.embedding_provider}"}
        import httpx

        url = f"{s.ollama_base_url.rstrip('/')}/api/tags"
        resp = httpx.get(url, timeout=5.0)
        ok = resp.status_code == 200
        return {
            "status": "ok" if ok else "error",
            "detail": None if ok else f"HTTP {resp.status_code}",
            "http_status": resp.status_code,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_api_health(base: str) -> dict[str, Any]:
    try:
        import httpx

        url = base.rstrip("/") + "/health"
        resp = httpx.get(url, timeout=5.0)
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        return {
            "status": "ok" if resp.status_code == 200 else "error",
            "http_status": resp.status_code,
            "body": body,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def main() -> int:
    p = argparse.ArgumentParser(description="运维一键巡检（JSON 输出）")
    p.add_argument("--no-pretty", action="store_true", help="单行 JSON")
    p.add_argument(
        "--api-url",
        default=None,
        help="若提供，则额外 GET {url}/health（如 http://127.0.0.1:8000 或经 Nginx 的地址）",
    )
    args = p.parse_args()

    checks: dict[str, Any] = {
        "postgresql": _check_postgresql(),
        "milvus": _check_milvus(),
        "redis": _check_redis(),
        "ollama": _check_ollama(),
    }
    if args.api_url:
        checks["api_health"] = _check_api_health(args.api_url)

    def is_bad(c: dict[str, Any]) -> bool:
        return c.get("status") == "error"

    success = not any(is_bad(v) for v in checks.values())
    out = {
        "success": success,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
    if args.no_pretty:
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
