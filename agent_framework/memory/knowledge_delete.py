"""
按 source_file 删除 PG + Milvus 中的知识块（与 scripts/kb 行为一致，供 API / TaskRunner 调用）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def milvus_delete_by_source_file(collection: str, source_file: str, dry_run: bool = False) -> int:
    from pymilvus import MilvusClient
    from agent_framework.config.settings import get_settings

    safe = source_file.replace("\\", "\\\\").replace('"', '\\"')
    expr = f'source_file == "{safe}"'

    s = get_settings()
    uri = f"http://{s.milvus_host}:{s.milvus_port}"
    token = f"{s.milvus_user}:{s.milvus_password}" if s.milvus_user and s.milvus_password else ""

    if dry_run:
        logger.info("[dry-run] Milvus delete expr: %s (collection=%s)", expr, collection)
        return -1

    try:
        mc = MilvusClient(uri=uri, token=token)
    except Exception as e:
        logger.error("Milvus 连接失败: %s", e)
        return 0

    if collection not in mc.list_collections():
        logger.warning("Milvus 无 collection=%s，跳过向量删除", collection)
        return 0

    try:
        res = mc.delete(collection_name=collection, filter=expr)
        mc.flush(collection_name=collection)
        cnt = 0
        if isinstance(res, dict):
            cnt = int(res.get("delete_count", res.get("delete_cnt", 0)) or 0)
        else:
            cnt = getattr(res, "delete_count", None) or getattr(res, "delete_cnt", None)
            if cnt is None and hasattr(res, "primary_keys") and res.primary_keys is not None:
                cnt = len(res.primary_keys)
            cnt = int(cnt or 0)
        n = cnt
    except Exception as e:
        logger.error("Milvus delete 失败 collection=%s expr=%s: %s", collection, expr, e)
        raise
    logger.info("Milvus 已删除 collection=%s source_file=%r 条数≈%s", collection, source_file, n)
    return n


def pg_delete_by_source_file(collection: str, source_file: str, dry_run: bool = False) -> int:
    from agent_framework.memory.pg_client import get_pg_client

    if dry_run:
        client = get_pg_client()
        with client.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM documents
                   WHERE collection = %s AND metadata->>'source_file' = %s""",
                (collection, source_file),
            )
            n = int(cur.fetchone()[0])
        logger.info(
            "[dry-run] 将 DELETE … collection=%s AND source_file=%r（%d 行）",
            collection,
            source_file,
            n,
        )
        return n
    client = get_pg_client()
    with client.cursor() as cur:
        cur.execute(
            """DELETE FROM documents
               WHERE collection = %s AND metadata->>'source_file' = %s""",
            (collection, source_file),
        )
        n = cur.rowcount if cur.rowcount is not None else 0
    logger.info("PG 已删除 collection=%s source_file=%r 行数=%s", collection, source_file, n)
    return int(n)


def delete_chunks_by_source_file(collection: str, source_file: str, *, dry_run: bool = False) -> None:
    """先 Milvus 再 PG，与 kb ingest --replace 一致。"""
    milvus_delete_by_source_file(collection, source_file, dry_run=dry_run)
    pg_delete_by_source_file(collection, source_file, dry_run=dry_run)
