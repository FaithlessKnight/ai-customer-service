from __future__ import annotations

import logging
from typing import Any

from agent_framework.memory import knowledge_delete

logger = logging.getLogger(__name__)


def pg_counts(collections: tuple[str, ...]) -> dict[str, int]:
    from agent_framework.memory.pg_client import get_pg_client

    client = get_pg_client()
    out: dict[str, int] = {}
    with client.cursor() as cur:
        for col in collections:
            cur.execute("SELECT COUNT(*) FROM documents WHERE collection = %s", (col,))
            out[col] = int(cur.fetchone()[0])
    return out


def pg_list_sources(collection: str, limit: int = 200) -> list[tuple[str, int]]:
    """返回 (source_file, chunk_count)，source_file 为空字符串的归为 (空)."""
    from agent_framework.memory.pg_client import get_pg_client

    client = get_pg_client()
    with client.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(metadata->>'source_file', ''), COUNT(*)
            FROM documents
            WHERE collection = %s
            GROUP BY 1
            ORDER BY COUNT(*) DESC
            LIMIT %s
            """,
            (collection, limit),
        )
        rows = cur.fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def pg_delete_by_collection(collection: str, dry_run: bool = False) -> int:
    from agent_framework.memory.pg_client import get_pg_client

    if dry_run:
        c = pg_counts((collection,))
        logger.info("[dry-run] 将 DELETE FROM documents WHERE collection=%s（当前 %d 行）", collection, c.get(collection, 0))
        return int(c.get(collection, 0))
    client = get_pg_client()
    with client.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE collection = %s", (collection,))
        n = cur.rowcount if cur.rowcount is not None else 0
    logger.info("PG 已删除 collection=%s 行数=%s", collection, n)
    return int(n)


def pg_delete_by_source_file(collection: str, source_file: str, dry_run: bool = False) -> int:
    return knowledge_delete.pg_delete_by_source_file(collection, source_file, dry_run=dry_run)


def milvus_num_entities(names: tuple[str, ...]) -> dict[str, int | str]:
    """
    各 collection 的 Milvus num_entities（近似值）。
    在 delete / flush 后可能与实际可检索实体数或 PG 行数不一致，勿单独用于对账。
    """
    from pymilvus import Collection, connections, utility
    from agent_framework.config.settings import get_settings

    s = get_settings()
    uri = f"http://{s.milvus_host}:{s.milvus_port}"
    token = f"{s.milvus_user}:{s.milvus_password}" if s.milvus_user and s.milvus_password else ""
    out: dict[str, int | str] = {}
    try:
        connections.connect("default", uri=uri, token=token)
        existing = set(utility.list_collections())
    except Exception as e:
        return {n: f"错误:{e}" for n in names}
    for name in names:
        if name not in existing:
            out[name] = 0
            continue
        try:
            col = Collection(name)
            col.flush()
            out[name] = int(col.num_entities)
        except Exception as e:
            out[name] = f"错误:{e}"
    return out


def milvus_drop_collection(collection: str, dry_run: bool = False) -> None:
    from pymilvus import MilvusClient
    from agent_framework.config.settings import get_settings

    if dry_run:
        logger.info("[dry-run] 将 drop Milvus collection=%s", collection)
        return
    s = get_settings()
    uri = f"http://{s.milvus_host}:{s.milvus_port}"
    token = f"{s.milvus_user}:{s.milvus_password}" if s.milvus_user and s.milvus_password else ""
    mc = MilvusClient(uri=uri, token=token)
    existing = set(mc.list_collections())
    if collection not in existing:
        logger.warning("Milvus 中不存在 collection=%s，跳过 drop", collection)
        return
    mc.drop_collection(collection)
    logger.info("已 drop Milvus collection=%s", collection)


def milvus_delete_by_source_file(collection: str, source_file: str, dry_run: bool = False) -> int:
    """按动态字段 source_file 删除 Milvus 中的实体（实现见 knowledge_delete）。"""
    return knowledge_delete.milvus_delete_by_source_file(collection, source_file, dry_run=dry_run)
