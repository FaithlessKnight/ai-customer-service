#!/usr/bin/env python3
"""
运维备份辅助：PostgreSQL 逻辑备份 / 恢复；Milvus 侧导出「集合列表 + 条数」元数据（非全量向量文件级备份）。

全量 Milvus 数据依赖 Docker 卷或官方备份工具；本脚本便于随 PG 逻辑库一起做变更前留痕。

用法（宿主机已安装 docker compose，且在仓库根执行）:
    python scripts/ops/backup.py pg-dump --out backups/agent_db.sql
    python scripts/ops/backup.py pg-dump --out backups/agent_db.sql.gz --gzip
    python scripts/ops/backup.py milvus-report --out backups/milvus_report.json
    python scripts/ops/backup.py pg-restore --in backups/agent_db.sql

容器内仅连接 DB 时，可直接用 psql/pg_dump 环境变量，不必使用本脚本的 docker 子命令。
"""
from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.yml"


def _run_pg_dump_docker(out_path: Path, gzip_out: bool) -> None:
    if not COMPOSE_FILE.is_file():
        raise SystemExit(f"未找到 {COMPOSE_FILE}，请在仓库根执行或使用本机 pg_dump。")
    cmd = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"',
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.decode("utf-8", errors="replace") or f"pg_dump exit {proc.returncode}")
    data = proc.stdout
    if gzip_out:
        out_path = out_path if str(out_path).endswith(".gz") else out_path.with_suffix(out_path.suffix + ".gz")
        out_path.write_bytes(gzip.compress(data))
    else:
        out_path.write_bytes(data)


def _run_pg_restore_docker(sql_path: Path) -> None:
    if not COMPOSE_FILE.is_file():
        raise SystemExit(f"未找到 {COMPOSE_FILE}")
    raw = sql_path.read_bytes()
    if sql_path.suffix == ".gz" or str(sql_path).endswith(".sql.gz"):
        raw = gzip.decompress(raw)
    cmd = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -v ON_ERROR_STOP=1',
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), input=raw, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise SystemExit(err or f"psql exit {proc.returncode}")


def _milvus_report() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pymilvus import MilvusClient
    from agent_framework.config.settings import get_settings

    s = get_settings()
    uri = f"http://{s.milvus_host}:{s.milvus_port}"
    token = f"{s.milvus_user}:{s.milvus_password}" if s.milvus_user and s.milvus_password else ""
    mc = MilvusClient(uri=uri, token=token)
    names = mc.list_collections()
    rows: dict[str, Any] = {}
    for name in sorted(names):
        try:
            stats = mc.get_collection_stats(collection_name=name)
            rows[name] = {"stats": stats}
        except Exception as e:
            rows[name] = {"error": str(e)}
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uri": uri,
        "collections": rows,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="PG / Milvus 备份辅助")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("pg-dump", help="docker compose exec postgres pg_dump 写入文件")
    p_dump.add_argument("--out", type=Path, required=True)
    p_dump.add_argument("--gzip", action="store_true", help="写入 gzip 压缩")

    p_rep = sub.add_parser("pg-restore", help="docker compose exec postgres psql 从 stdin 灌入")
    p_rep.add_argument("--in", dest="in_path", type=Path, required=True, help=".sql 或 .sql.gz")

    p_mv = sub.add_parser("milvus-report", help="导出 Milvus 集合列表与 get_collection_stats")
    p_mv.add_argument("--out", type=Path, required=True)

    args = p.parse_args()
    if args.cmd == "pg-dump":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        _run_pg_dump_docker(args.out, args.gzip)
        print(f"OK pg_dump -> {args.out.resolve()}")
        return 0
    if args.cmd == "pg-restore":
        _run_pg_restore_docker(args.in_path)
        print("OK pg_restore")
        return 0
    if args.cmd == "milvus-report":
        report = _milvus_report()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OK milvus-report -> {args.out.resolve()}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
