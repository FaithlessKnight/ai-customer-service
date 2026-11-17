"""
标准 products / sales_script 全量重建（由 kb rebuild 与 scripts/rebuild_standard_knowledge 共用）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 本文件位于 scripts/kb/rebuild_std.py → 仓库根为 parents[2]
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("rebuild_standard_knowledge")

REPO_ROOT = _REPO
SALES_DIR = REPO_ROOT / "data" / "sales_scripts"
PRODUCT_DIR = REPO_ROOT / "product"


def _collect_product_md() -> list[Path]:
    if not PRODUCT_DIR.is_dir():
        return []
    found: list[Path] = []
    for sub in sorted(PRODUCT_DIR.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        for md in sorted(sub.glob("*_知识库.md")):
            found.append(md)
    return found


def _default_product_docx() -> Path | None:
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        return None
    candidates = list(desktop.glob("*.docx"))
    if not candidates:
        return None
    for p in candidates:
        name = p.stem
        if "沐浴乳" in name or "新肌秀" in name or "舒净" in name:
            return p
    if len(candidates) == 1:
        return candidates[0]
    return None


def _pg_counts(collections: tuple[str, ...]) -> dict[str, int]:
    from agent_framework.memory.pg_client import get_pg_client

    client = get_pg_client()
    out: dict[str, int] = {}
    with client.cursor() as cur:
        for col in collections:
            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE collection = %s",
                (col,),
            )
            out[col] = int(cur.fetchone()[0])
    return out


def _pg_delete_collections(collections: tuple[str, ...]) -> dict[str, int]:
    from agent_framework.memory.pg_client import get_pg_client

    client = get_pg_client()
    deleted: dict[str, int] = {}
    with client.cursor() as cur:
        for col in collections:
            cur.execute("DELETE FROM documents WHERE collection = %s", (col,))
            deleted[col] = cur.rowcount if cur.rowcount is not None else 0
    logger.info("PG 已删除: %s", deleted)
    return deleted


def _milvus_drop_collections(names: tuple[str, ...]) -> None:
    from pymilvus import MilvusClient
    from agent_framework.config.settings import get_settings

    s = get_settings()
    uri = f"http://{s.milvus_host}:{s.milvus_port}"
    token = (
        f"{s.milvus_user}:{s.milvus_password}"
        if s.milvus_user and s.milvus_password
        else ""
    )
    mc = MilvusClient(uri=uri, token=token)
    existing = set(mc.list_collections())
    for name in names:
        if name not in existing:
            logger.warning("Milvus 中不存在 collection=%s，跳过 drop", name)
            continue
        try:
            mc.drop_collection(name)
            logger.info("已 drop Milvus collection=%s", name)
        except Exception as e:
            logger.error("drop Milvus 失败 %s: %s", name, e)
            raise


def _milvus_num_entities(names: tuple[str, ...]) -> dict[str, int | str]:
    from pymilvus import Collection, connections, utility
    from agent_framework.config.settings import get_settings

    s = get_settings()
    uri = f"http://{s.milvus_host}:{s.milvus_port}"
    token = (
        f"{s.milvus_user}:{s.milvus_password}"
        if s.milvus_user and s.milvus_password
        else ""
    )
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


def _ingest_products(sources: list[Path]) -> None:
    from agent_framework.memory.bm25_retriever import BM25Retriever
    from agent_framework.memory.hybrid_retriever import HybridRetriever
    from agent_framework.memory.vector_store import MilvusRetriever
    from agent_framework.pipeline.ingest_pipeline import IngestPipeline

    valid = [p for p in sources if p.is_file()]
    missing = [p for p in sources if not p.is_file()]
    for p in missing:
        logger.warning("产品文档不存在，已跳过: %s", p)
    if not valid:
        raise FileNotFoundError(f"没有可用的产品文档可入库: {sources}")

    retriever = HybridRetriever(
        vector_retriever=MilvusRetriever(collection_name="products"),
        bm25_retriever=BM25Retriever(source="postgresql", collection="products"),
    )
    pipeline = IngestPipeline(retriever=retriever)
    result = pipeline.ingest_files(
        valid,
        extra_metadata={
            "doc_type": "product",
            "category": "沐浴乳",
            "product_name": "新肌秀舒净雅香沐浴乳",
        },
    )
    logger.info(result.summary())


def _ingest_sales_scripts() -> None:
    import importlib.util

    mod_path = REPO_ROOT / "scripts" / "ingest_sales_scripts.py"
    spec = importlib.util.spec_from_file_location("ingest_sales_scripts", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {mod_path}")
    ing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ing)

    files = ing.collect_files(SALES_DIR)
    if not files:
        raise SystemExit(f"未找到话术 .md: {SALES_DIR}")
    ing.ingest(files, "sales_script")
    logger.info("话术入库完成: %d 个文件", len(files))


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "清理并重建标准 products / sales_script 知识库。\n"
            "推荐入口: python scripts/kb.py rebuild …\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", help="只统计/打印计划，不删除、不入库")
    p.add_argument("--product-docx", type=Path, default=None,
                   help="兜底用：当仓库 product/*/_知识库.md 不存在时，从此 docx 入库")
    p.add_argument("--products", action="store_true",
                   help="重建 products collection（仓库 product/*/*_知识库.md → products）")
    p.add_argument("--scripts", action="store_true",
                   help="重建 sales_script collection（data/sales_scripts/场景*.md → sales_script）")
    p.add_argument("--skip-products", action="store_true", help="[已废弃，保留兼容] 不再使用")
    p.add_argument("--skip-scripts", action="store_true", help="[已废弃，保留兼容] 不再使用")
    p.add_argument(
        "--collections",
        default=None,
        help="[高级用法] 要清理的 PG/Milvus collection，逗号分隔。",
    )
    args = p.parse_args()

    do_products = args.products
    do_scripts = args.scripts
    if not do_products and not do_scripts:
        if args.skip_products and not args.skip_scripts:
            do_scripts = True
        elif args.skip_scripts and not args.skip_products:
            do_products = True
        else:
            raise SystemExit(
                "未指定要重建的 collection。请显式加 --products 和/或 --scripts；\n"
                "或使用 --dry-run 查看现状。\n"
                "（旧版默认会同时重建两者，已禁用以避免误删。）"
            )

    if args.collections:
        cols = tuple(c.strip() for c in args.collections.split(",") if c.strip())
    else:
        chosen: list[str] = []
        if do_products:
            chosen.append("products")
        if do_scripts:
            chosen.append("sales_script")
        cols = tuple(chosen)

    product_sources: list[Path] = []
    if do_products:
        product_sources = _collect_product_md()
        if product_sources:
            logger.info("使用仓库产品 .md（%d 份）作为入库源:", len(product_sources))
            for p_ in product_sources:
                logger.info("  - %s", p_.relative_to(REPO_ROOT))
        else:
            docx = args.product_docx or _default_product_docx()
            if docx:
                logger.info("仓库 product/ 未找到 .md，回退到 docx: %s", docx)
                product_sources = [docx]

    logger.info("=== 当前存量（PG 行数 / Milvus row_count）===")
    all_known = ("products", "sales_script")
    try:
        pg_c = _pg_counts(all_known)
        logger.info("PostgreSQL: %s", pg_c)
    except Exception as e:
        logger.warning("读取 PG 失败: %s", e)
    try:
        mv_c = _milvus_num_entities(all_known)
        logger.info("Milvus(num_entities): %s", mv_c)
    except Exception as e:
        logger.warning("读取 Milvus 失败: %s", e)

    logger.info("=== 本次将重建: %s（其它 collection 不动）===", cols)

    if args.dry_run:
        logger.info("[dry-run] 将 DELETE PG + drop Milvus 仅限: %s", cols)
        if do_products:
            if product_sources:
                logger.info("[dry-run] 产品入库文件: %s", [str(p_) for p_ in product_sources])
            else:
                logger.info("[dry-run] 产品入库文件: (未找到，请传 --product-docx 或在 product/<name>/ 下放 *_知识库.md)")
        if do_scripts:
            logger.info("[dry-run] 话术目录: %s", SALES_DIR)
        return

    _pg_delete_collections(cols)
    _milvus_drop_collections(cols)

    if do_products:
        if not product_sources:
            raise SystemExit(
                "未找到产品入库源。请在 product/<产品名>/ 下放置「*_知识库.md」，"
                "或将 docx 放桌面，或指定: --product-docx \"完整路径\""
            )
        logger.info("开始入库产品: %d 份文档", len(product_sources))
        _ingest_products([p_.resolve() for p_ in product_sources])

    if do_scripts:
        logger.info("开始入库话术…")
        _ingest_sales_scripts()

    logger.info("=== 重建完成 ===")
    try:
        pg_final = _pg_counts(cols)
        mv_final = _milvus_num_entities(cols)
        logger.info("PostgreSQL: %s", pg_final)
        logger.info("Milvus(num_entities): %s", mv_final)
        for col in cols:
            pgc = pg_final.get(col, 0)
            mvc = mv_final.get(col, 0)
            if isinstance(mvc, int) and pgc > 0 and mvc != pgc:
                hint = (
                    "python scripts/kb.py ingest data/sales_scripts --collection sales_script"
                    if col == "sales_script"
                    else "python scripts/kb.py rebuild --products"
                )
                logger.warning(
                    "Milvus collection=%s 向量条数=%d 与 PG 条数=%d 不一致（常见原因：Embedding 服务失败）。"
                    "可重试: %s",
                    col,
                    mvc,
                    pgc,
                    hint,
                )
    except Exception as e:
        logger.warning("复查存量失败: %s", e)


if __name__ == "__main__":
    main()
