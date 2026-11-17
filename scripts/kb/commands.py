from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

from kb.collection_map import VALID_COLLECTIONS, infer_collection
from kb import chunk_tracker

logger = logging.getLogger("kb.commands")

REPO_ROOT = Path(__file__).resolve().parents[2]

# products 对应 Milvus schema 要求写入 category；测试中出现缺省导致向量失败、PG 仍写入。
_ENV_PRODUCTS_CATEGORY = "KB_PRODUCTS_DEFAULT_CATEGORY"


def _effective_products_category(explicit: str | None) -> str:
    """命令行 --category 优先，否则读环境变量 KB_PRODUCTS_DEFAULT_CATEGORY。"""
    if (explicit or "").strip():
        return (explicit or "").strip()
    return (os.environ.get(_ENV_PRODUCTS_CATEGORY) or "").strip()


def _load_ingest_sales_module():
    mod_path = REPO_ROOT / "scripts" / "ingest_sales_scripts.py"
    spec = importlib.util.spec_from_file_location("ingest_sales_scripts", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_status(args: argparse.Namespace) -> int:
    cols = tuple(
        c.strip()
        for c in (args.collections or "products,sales_script").split(",")
        if c.strip()
    )
    for c in cols:
        if c not in VALID_COLLECTIONS:
            logger.error("未知 collection: %s", c)
            return 1
    try:
        pg = chunk_tracker.pg_counts(cols)
        logger.info("PostgreSQL 行数: %s", pg)
    except Exception as e:
        logger.error("读取 PG 失败: %s", e)
        return 1
    try:
        mv = chunk_tracker.milvus_num_entities(cols)
        logger.info("Milvus num_entities: %s", mv)
        logger.warning(
            "Milvus num_entities 在 delete/replace 后常为近似值，可能与 PG 不一致；"
            "按文件核对请用: kb.py list --collection <name>"
        )
    except Exception as e:
        logger.warning("读取 Milvus 失败: %s", e)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.collection not in VALID_COLLECTIONS:
        logger.error("未知 collection: %s", args.collection)
        return 1
    rows = chunk_tracker.pg_list_sources(args.collection, limit=args.limit)
    if not rows:
        logger.info("collection=%s 无数据或 source_file 全为空", args.collection)
        return 0
    logger.info("collection=%s 文档来源（PG metadata->source_file）:", args.collection)
    for name, cnt in rows:
        logger.info("  %6d  %s", cnt, name or "(空)")
    logger.info("（以上为 PG 聚合，可作 delete/replace 后与 Milvus 核对的主依据）")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    col = args.collection
    if col not in VALID_COLLECTIONS:
        logger.error("未知 collection: %s", col)
        return 1
    if args.source:
        if args.dry_run:
            chunk_tracker.pg_delete_by_source_file(col, args.source, dry_run=True)
            chunk_tracker.milvus_delete_by_source_file(col, args.source, dry_run=True)
            return 0
        chunk_tracker.milvus_delete_by_source_file(col, args.source, dry_run=False)
        chunk_tracker.pg_delete_by_source_file(col, args.source, dry_run=False)
        logger.info("已按 source_file=%r 删除 collection=%s", args.source, col)
        return 0

    if args.dry_run:
        chunk_tracker.pg_delete_by_collection(col, dry_run=True)
        chunk_tracker.milvus_drop_collection(col, dry_run=True)
        return 0
    chunk_tracker.pg_delete_by_collection(col, dry_run=False)
    chunk_tracker.milvus_drop_collection(col, dry_run=False)
    logger.info("已清空 collection=%s（PG 全删 + Milvus drop）", col)
    return 0


def cmd_rebuild(argv: list[str]) -> int:
    """透传 flags 给 rebuild_std（其 argparse 读 sys.argv[1:]）。"""
    old = sys.argv[:]
    sys.argv = ["rebuild_std", *(argv or [])]
    try:
        from kb import rebuild_std

        rebuild_std.main()
        return 0
    except SystemExit as e:
        c = e.code
        if c is None:
            return 0
        return int(c) if isinstance(c, int) else 1
    finally:
        sys.argv = old


def _ingest_sales_files(
    paths: list[Path],
    collection: str,
    replace: bool,
    dry_run: bool,
    only: str | None = None,
    skip: str | None = None,
) -> int:
    ing = _load_ingest_sales_module()
    files = [p for p in paths if p.suffix.lower() == ".md" and p.is_file()]
    if only:
        onlys = {x.strip() for x in only.split(",") if x.strip()}
        files = [f for f in files if any(f.name.startswith(p) for p in onlys)]
    if skip:
        skips = {x.strip() for x in skip.split(",") if x.strip()}
        files = [f for f in files if not any(f.name.startswith(p) for p in skips)]
    if not files:
        logger.error("未找到可入库的 .md 文件")
        return 1
    if dry_run:
        ing.dry_run(files)
        return 0
    if replace:
        for f in files:
            sf = f.name
            chunk_tracker.milvus_delete_by_source_file(collection, sf, dry_run=False)
            chunk_tracker.pg_delete_by_source_file(collection, sf, dry_run=False)
    ing.ingest(files, collection)
    return 0


def _ingest_pipeline_files(
    paths: list[Path],
    collection: str,
    replace: bool,
    dry_run: bool,
    doc_type: str,
    category: str,
) -> int:
    from agent_framework.memory.bm25_retriever import BM25Retriever
    from agent_framework.memory.hybrid_retriever import HybridRetriever
    from agent_framework.memory.vector_store import MilvusRetriever
    from agent_framework.pipeline.ingest_pipeline import IngestPipeline

    valid = [p.resolve() for p in paths if p.is_file()]
    if not valid:
        logger.error("没有有效文件")
        return 1
    if dry_run:
        retriever = HybridRetriever(
            vector_retriever=MilvusRetriever(collection_name=collection),
            bm25_retriever=BM25Retriever(source="postgresql", collection=collection),
        )
        pipeline = IngestPipeline(retriever=retriever)
        for fp in valid[:3]:
            chunks = pipeline.preview(fp, max_chunks=5)
            logger.info("[dry-run] %s 预览块数(最多5): %d", fp.name, len(chunks))
        return 0

    if replace:
        for fp in valid:
            sf = fp.name
            chunk_tracker.milvus_delete_by_source_file(collection, sf, dry_run=False)
            chunk_tracker.pg_delete_by_source_file(collection, sf, dry_run=False)

    retriever = HybridRetriever(
        vector_retriever=MilvusRetriever(collection_name=collection),
        bm25_retriever=BM25Retriever(source="postgresql", collection=collection),
    )
    extra: dict[str, Any] = {"doc_type": doc_type}
    if category:
        extra["category"] = category
    if len(valid) == 1:
        extra.setdefault("product_name", valid[0].stem.replace("_知识库", ""))
    pipeline = IngestPipeline(retriever=retriever)
    result = pipeline.ingest_files(valid, extra_metadata=extra)
    logger.info(result.summary())
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    if not target.exists():
        logger.error("路径不存在: %s", target)
        return 1

    try:
        collection = infer_collection(target, args.collection)
    except ValueError as e:
        logger.error("%s", e)
        return 1
    if collection not in VALID_COLLECTIONS:
        logger.error("非法 collection: %s", collection)
        return 1

    cat_pipeline = _effective_products_category(args.category)
    if collection == "products":
        if not cat_pipeline:
            logger.error(
                "collection=products 时必须提供 --category（Milvus metadata 必填，缺省会导致向量写入失败而 PG 仍插入）。"
                "示例: --category \"沐浴乳\"。或设置环境变量 %s。",
                _ENV_PRODUCTS_CATEGORY,
            )
            return 1
        if not (args.category or "").strip() and cat_pipeline:
            logger.info("使用环境变量 %s=%r 作为 category", _ENV_PRODUCTS_CATEGORY, cat_pipeline)

    if target.is_dir():
        if collection == "sales_script":
            ing = _load_ingest_sales_module()
            md_files = ing.collect_files(target)
            return _ingest_sales_files(
                md_files,
                collection,
                args.replace,
                args.dry_run,
                getattr(args, "only", None),
                getattr(args, "skip", None),
            )
        paths = [p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".docx", ".pdf", ".xlsx", ".xls", ".csv"}]
        if not paths:
            logger.error("目录下无支持的文档类型")
            return 1
        return _ingest_pipeline_files(
            paths,
            collection,
            args.replace,
            args.dry_run,
            args.doc_type,
            cat_pipeline if collection == "products" else (args.category or "").strip(),
        )

    paths = [target]
    if collection == "sales_script" and target.suffix.lower() == ".md":
        return _ingest_sales_files(
            paths,
            collection,
            args.replace,
            args.dry_run,
            getattr(args, "only", None),
            getattr(args, "skip", None),
        )
    return _ingest_pipeline_files(
        paths,
        collection,
        args.replace,
        args.dry_run,
        args.doc_type,
        cat_pipeline if collection == "products" else (args.category or "").strip(),
    )


def cmd_staging_upload(args: argparse.Namespace) -> int:
    from agent_framework.pipeline.staging import StagingArea

    p = Path(args.file).resolve()
    if not p.is_file():
        logger.error("文件不存在: %s", p)
        return 1
    st = StagingArea()
    coll = args.collection
    cat_eff = _effective_products_category(args.category)
    if coll == "products":
        if not cat_eff:
            logger.error(
                "staging upload 至 collection=products 时必须提供 --category，或设置环境变量 %s。",
                _ENV_PRODUCTS_CATEGORY,
            )
            return 1
        if not (args.category or "").strip() and cat_eff:
            logger.info("使用环境变量 %s=%r 作为 category", _ENV_PRODUCTS_CATEGORY, cat_eff)
    meta: dict[str, Any] = {"collection": coll, "doc_type": args.doc_type}
    if coll == "products":
        meta["category"] = cat_eff
    elif args.category:
        meta["category"] = args.category
    sf = st.upload(str(p), metadata=meta)
    logger.info("已上传暂存区 file_id=%s path=%s", sf.file_id, sf.staged_path)
    return 0


def cmd_staging_list(_args: argparse.Namespace) -> int:
    from agent_framework.pipeline.staging import StagingArea

    st = StagingArea()
    st.print_status()
    return 0


def cmd_staging_process(args: argparse.Namespace) -> int:
    from agent_framework.pipeline.task_runner import TaskRunner

    runner = TaskRunner()
    if args.dry_run:
        pending = runner.staging.list_files(status="pending")
        if not pending:
            logger.info("无 pending 文件")
            return 0
        for sf in pending[:20]:
            runner.dry_run(sf.file_id, max_chunks=args.preview_chunks)
        return 0
    runner.run_all()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kb.py",
        description="知识库统一管理 CLI（PostgreSQL + Milvus）",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("status", help="查看 PG / Milvus 各 collection 条数")
    ps.add_argument(
        "--collections",
        default="products,sales_script",
        help="逗号分隔 collection 列表",
    )
    ps.set_defaults(func=cmd_status)

    pl = sub.add_parser("list", help="列出某 collection 下各 source_file 的 chunk 数")
    pl.add_argument("--collection", required=True)
    pl.add_argument("--limit", type=int, default=200)
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("delete", help="删除 PG+Milvus 数据")
    pd.add_argument("--collection", required=True)
    pd.add_argument("--source", default=None, help="仅删除 metadata.source_file 匹配的块；不传则清空整个 collection")
    pd.add_argument("--dry-run", action="store_true")
    pd.set_defaults(func=cmd_delete)

    pr = sub.add_parser(
        "rebuild",
        help="全量重建 products / sales_script（等同 rebuild_standard_knowledge）",
    )
    pr.add_argument(
        "rebuild_argv",
        nargs=argparse.REMAINDER,
        default=[],
        help="透传参数，如 --products --scripts --dry-run",
    )
    pr.set_defaults(func=lambda a: cmd_rebuild(getattr(a, "rebuild_argv", None) or []))

    pi = sub.add_parser("ingest", help="增量入库文件或目录")
    pi.add_argument("path", help="文件或目录路径")
    pi.add_argument("--collection", default=None, help="强制指定 collection（不推断时必填）")
    pi.add_argument("--replace", action="store_true", help="先按 source_file 删除旧 chunks 再写入")
    pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--doc-type", default="product", help="写入 metadata.doc_type（pipeline 路径）")
    pi.add_argument(
        "--category",
        default="",
        help="写入 metadata.category；collection=products 时必填，或设环境变量 KB_PRODUCTS_DEFAULT_CATEGORY",
    )
    pi.add_argument(
        "--only",
        default=None,
        help="[sales_script 目录] 仅处理 doc_id 前缀，如 01 或 01,02",
    )
    pi.add_argument("--skip", default=None, help="[sales_script 目录] 跳过 doc_id 前缀，如 01")
    pi.set_defaults(func=cmd_ingest)

    pst = sub.add_parser("staging", help="暂存区")
    sst = pst.add_subparsers(dest="st_action", required=True)

    pu = sst.add_parser("upload", help="复制文件到暂存区 pending")
    pu.add_argument("file")
    pu.add_argument("--collection", default="products")
    pu.add_argument("--doc-type", default="product")
    pu.add_argument(
        "--category",
        default="",
        help="写入 metadata.category；collection=products 时必填，或设 KB_PRODUCTS_DEFAULT_CATEGORY",
    )
    pu.set_defaults(func=cmd_staging_upload)

    pls = sst.add_parser("list", help="打印暂存区状态")
    pls.set_defaults(func=cmd_staging_list)

    ppr = sst.add_parser("process", help="处理全部 pending（TaskRunner.run_all）")
    ppr.add_argument("--dry-run", action="store_true", help="仅对 pending 做 dry_run 预览")
    ppr.add_argument("--preview-chunks", type=int, default=5)
    ppr.set_defaults(func=cmd_staging_process)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args))
