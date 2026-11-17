"""
清理 LangGraph checkpoint 历史快照

策略：
1. 每个 session 只保留最新一条 checkpoint（其余历史快照无用）
2. 超过 INACTIVE_DAYS 天未活跃的 session 整体删除

相关表：checkpoints / checkpoint_blobs / checkpoint_writes
  - checkpoints.checkpoint_id 是每轮快照的唯一ID
  - checkpoint_blobs / checkpoint_writes 通过 (thread_id, checkpoint_ns, checkpoint_id) 关联

用法：
    python scripts/cleanup_checkpoints.py --dry-run     # 预览，不实际删除
    python scripts/cleanup_checkpoints.py               # 执行清理
    python scripts/cleanup_checkpoints.py --inactive-days 7  # 自定义过期天数
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("cleanup_checkpoints")

INACTIVE_DAYS = 30  # 默认：超过30天未活跃的 session 整体删除


def get_conn():
    from dotenv import dotenv_values
    cfg = dotenv_values(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    import psycopg2
    return psycopg2.connect(
        host=cfg["PG_HOST"], port=cfg["PG_PORT"],
        user=cfg["PG_USER"], password=cfg["PG_PASSWORD"],
        dbname=cfg["PG_DATABASE"],
    )


def cleanup(dry_run: bool = True, inactive_days: int = INACTIVE_DAYS) -> None:
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # ── 统计当前状态 ──────────────────────────────────
        cur.execute("SELECT COUNT(*) FROM checkpoints")
        total_before = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints")
        sessions_before = cur.fetchone()[0]
        logger.info("当前: %d 条快照 / %d 个 session", total_before, sessions_before)

        # ── Step1: 找出每个 session 最新的 checkpoint_id ──
        # checkpoint_id 是 UUIDv1，时间戳编码在其中，直接取 MAX 即最新
        cur.execute("""
            SELECT thread_id, checkpoint_ns, MAX(checkpoint_id) AS latest_id
            FROM checkpoints
            GROUP BY thread_id, checkpoint_ns
        """)
        latest_map = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        logger.info("找到 %d 个 (thread_id, ns) 组合", len(latest_map))

        # ── Step2: 删除每个 session 非最新的历史快照 ───────
        stale_total = 0
        for (thread_id, ns), latest_id in latest_map.items():
            cur.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id != %s",
                (thread_id, ns, latest_id),
            )
            stale_count = cur.fetchone()[0]
            if stale_count == 0:
                continue
            stale_total += stale_count
            if not dry_run:
                # checkpoint_writes 通过 checkpoint_id 关联，直接删非最新的
                cur.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id != %s",
                    (thread_id, ns, latest_id),
                )
                # checkpoint_blobs 无 checkpoint_id，通过 channel+version 间接清理：
                # 删除 checkpoints 后，孤立的 blobs 由定期 VACUUM 或单独清理处理
                # 此处只删 checkpoints 主表的历史快照
                cur.execute(
                    "DELETE FROM checkpoints WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id != %s",
                    (thread_id, ns, latest_id),
                )

        logger.info("%s 历史快照: %d 条", "【预览】将删除" if dry_run else "已删除", stale_total)

        # ── Step3: 删除超过 inactive_days 天未活跃的整个 session ──
        # 通过 checkpoints.metadata->>'step' 和 checkpoint_id 时间戳推断活跃时间
        # checkpoint_id 是 UUIDv1：前32bit 是时间戳（100ns 单位，自1582年起）
        # 换算：unix_ts = (uuid_time - 0x01b21dd213814000) / 1e7
        cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days)
        # 用 pg 的 uuid_v1_time 或直接解析 checkpoint 里的 ts
        # 简化方案：读 checkpoint jsonb 里的 ts 字段（LangGraph 写入时带 created_at）
        cur.execute("""
            SELECT DISTINCT c.thread_id
            FROM checkpoints c
            WHERE (c.checkpoint->>'ts')::timestamptz < %s
              AND c.thread_id NOT IN (
                  SELECT DISTINCT thread_id FROM checkpoints
                  WHERE (checkpoint->>'ts')::timestamptz >= %s
              )
        """, (cutoff, cutoff))
        inactive_sessions = [r[0] for r in cur.fetchall()]
        logger.info("%s 不活跃 session（>%d天）: %d 个",
                    "【预览】将删除" if dry_run else "已删除", inactive_days, len(inactive_sessions))

        if inactive_sessions and not dry_run:
            for tid in inactive_sessions:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s", (tid,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id=%s", (tid,))
            logger.info("已清理 %d 个不活跃 session", len(inactive_sessions))

        if not dry_run:
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM checkpoints")
            total_after = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints")
            sessions_after = cur.fetchone()[0]
            logger.info("清理完成: %d → %d 条（删除 %d 条），%d → %d 个 session",
                        total_before, total_after, total_before - total_after,
                        sessions_before, sessions_after)
        else:
            logger.info("【dry-run 模式】未实际删除任何数据，加 --no-dry-run 执行")

    except Exception as e:
        conn.rollback()
        logger.error("清理失败，已回滚: %s", e, exc_info=True)
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理 LangGraph checkpoint 历史快照")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="预览模式，不实际删除（默认）")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="执行实际删除")
    parser.add_argument("--inactive-days", type=int, default=INACTIVE_DAYS,
                        help=f"超过N天未活跃的session整体删除（默认{INACTIVE_DAYS}天）")
    args = parser.parse_args()

    cleanup(dry_run=args.dry_run, inactive_days=args.inactive_days)
