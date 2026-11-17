"""
话术知识库入库脚本

将「话术知识库/*.md」按章节切分后写入 sales_script collection。
保持每个二级标题（##）章节为一个完整 chunk，避免被默认 splitter 切碎。

每份文档须在「适用场景标签」表中至少提供 scene、journey_stage（与 CRM / /cs 一致），
可选扩展列见 data/sales_scripts/README_话术文档规范.md。

用法：
    python scripts/ingest_sales_scripts.py --source data/sales_scripts --dry-run
    python scripts/ingest_sales_scripts.py --source data/sales_scripts --collection sales_script
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest_sales_scripts")

# ── 表格字段名映射：Markdown 表格里的中文/英文字段 → metadata key ─────
_META_KEYS = {
    "scene", "journey_stage", "trigger_condition", "intent_level_range",
    "required_slots", "allowed_actions", "forbidden_actions", "next_stage",
}

_INT_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-~–]\s*(\d+)\s*$")
_BODY_HEADER_RE = re.compile(
    r"^(第[0-9一二三四五六七八九十]+轮[:：].*|[0-9]+[\.、]\s*.+|客户.*话术.*|【[^】]+】)$"
)


def _split_body_blocks(body_lines: list[str]) -> list[tuple[str, str]]:
    """
    将「二、话术正文」细粒度切分为多个子块，降低单 chunk 体积。
    优先按轮次/编号小节/显式标题切分。
    """
    blocks: list[tuple[str, str]] = []
    current_title = "导语"
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            blocks.append((current_title, text))
        buf = []

    for ln in body_lines:
        s = ln.strip()
        # 分隔线作为边界，不作为内容
        if s == "---":
            flush()
            continue
        # 轮次/编号/标题行触发新块
        if _BODY_HEADER_RE.match(s):
            flush()
            current_title = s
            continue
        buf.append(ln)
    flush()

    # 防守：异常情况下至少返回整段
    if not blocks:
        whole = "\n".join(body_lines).strip()
        if whole:
            return [("正文", whole)]
    return blocks


def parse_doc(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    解析一份话术文档：
      返回 (doc_level_metadata, chunk_list)
      chunk = {"content": str, "metadata": {...}}
    """
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    # 一级标题
    title = ""
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            break

    doc_id = path.stem.split("_", 1)[0]  # "01"
    doc_meta: dict[str, Any] = {
        "doc_type": "sales_script",
        "doc_id": doc_id,
        "doc_title": title,
        "persona": "小新",
        "source_file": path.name,
    }

    # 解析「适用场景标签」表格
    in_table = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("| 字段") or s.startswith("|字段"):
            in_table = True
            continue
        if in_table:
            if not s.startswith("|"):
                in_table = False
                continue
            # 跳过分隔行 |---|---|
            if set(s.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2:
                continue
            key, val = cells[0], cells[1]
            if key in _META_KEYS:
                if key == "intent_level_range":
                    m = _INT_RANGE_RE.match(val)
                    if m:
                        doc_meta["intent_level_min"] = int(m.group(1))
                        doc_meta["intent_level_max"] = int(m.group(2))
                    else:
                        doc_meta["intent_level_range"] = val
                elif key == "scene":
                    # "any（通用模块）" → "any"
                    doc_meta["scene"] = "any" if val.startswith("any") else val
                else:
                    doc_meta[key] = val

    if "scene" not in doc_meta or "journey_stage" not in doc_meta:
        logger.warning(
            "%s: 适用场景标签缺少 scene 或 journey_stage，检索过滤可能失效（见 README_话术文档规范）",
            path.name,
        )

    # 按二级标题切分
    chunks: list[dict[str, Any]] = []
    current_section: str | None = None
    current_index: int = 0
    buf: list[str] = []

    def flush():
        if current_section is None or not buf:
            return
        # 仅对「话术正文」做细粒度切分；其他章节保持整块
        if "话术正文" in current_section:
            sub_blocks = _split_body_blocks(buf)
            for sub_idx, (sub_title, sub_body) in enumerate(sub_blocks, start=1):
                content = f"{title} > {current_section} > {sub_title}\n\n{sub_body}"
                chunks.append({
                    "content": content,
                    "metadata": {
                        **doc_meta,
                        "section": current_section,
                        "section_index": current_index,
                        "sub_section": sub_title,
                        "sub_index": sub_idx,
                        "char_count": len(content),
                    },
                })
            return

        body = "\n".join(buf).strip()
        if not body:
            return
        content = f"{title} > {current_section}\n\n{body}"
        chunks.append({
            "content": content,
            "metadata": {
                **doc_meta,
                "section": current_section,
                "section_index": current_index,
                "char_count": len(content),
            },
        })

    for ln in lines:
        if ln.startswith("## "):
            flush()
            buf = []
            current_section = ln[3:].strip()
            current_index += 1
        else:
            if current_section is not None:
                buf.append(ln)
    flush()

    return doc_meta, chunks


def collect_files(source: Path) -> list[Path]:
    files = sorted(source.glob("*.md"))
    return [f for f in files if not f.name.startswith("README")]


def dry_run(files: list[Path]) -> None:
    total_chunks = 0
    for f in files:
        meta, chunks = parse_doc(f)
        print(f"\n=== {f.name} ===")
        print(f"  doc_id={meta.get('doc_id')} title={meta.get('doc_title')}")
        print(f"  scene={meta.get('scene')} journey_stage={meta.get('journey_stage')} "
              f"intent=[{meta.get('intent_level_min')}, {meta.get('intent_level_max')}]")
        print(f"  trigger={meta.get('trigger_condition', '')[:60]}")
        print(f"  chunks={len(chunks)}:")
        for c in chunks:
            preview = c["content"].replace("\n", " ")[:80]
            print(f"    [{c['metadata']['section_index']}] {c['metadata']['section']} "
                  f"({c['metadata']['char_count']}字) {preview}")
        total_chunks += len(chunks)
    print(f"\n>>> 共 {len(files)} 份文档，{total_chunks} 个 chunk")


def ingest(files: list[Path], collection: str) -> None:
    from agent_framework.memory.hybrid_retriever import HybridRetriever
    from agent_framework.memory.vector_store import MilvusRetriever
    from agent_framework.memory.bm25_retriever import BM25Retriever

    retriever = HybridRetriever(
        vector_retriever=MilvusRetriever(collection_name=collection),
        bm25_retriever=BM25Retriever(source="postgresql", collection=collection),
    )

    all_texts: list[str] = []
    all_metas: list[dict[str, Any]] = []
    for f in files:
        _, chunks = parse_doc(f)
        for c in chunks:
            all_texts.append(c["content"])
            all_metas.append(c["metadata"])
        logger.info("解析 %s -> %d chunks", f.name, len(chunks))

    logger.info("准备写入 collection=%s, 共 %d chunks", collection, len(all_texts))
    BATCH = 32
    written = 0
    for i in range(0, len(all_texts), BATCH):
        batch_t = all_texts[i:i + BATCH]
        batch_m = all_metas[i:i + BATCH]
        ids = retriever.add_documents(batch_t, batch_m)
        written += len(ids) if ids else len(batch_t)
        logger.info("批次 %d-%d 写入完成", i, i + len(batch_t))
    logger.info("✓ 入库完成: collection=%s 写入 %d chunks", collection, written)


def main():
    """兼容入口：委托 kb.py ingest（路径推断 sales_script）。"""
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="话术 .md 文件所在目录")
    p.add_argument("--collection", default="sales_script")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", default=None, help="仅处理指定 doc_id 前缀，如 01 或 01,02,05")
    p.add_argument("--skip", default=None, help="跳过指定 doc_id 前缀，如 01")
    args = p.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    root = scripts_dir.parent
    for d in (root, scripts_dir):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))

    from kb.commands import main as kb_main

    src = str(Path(args.source).resolve())
    argv = ["ingest", src, "--collection", args.collection]
    if args.dry_run:
        argv.append("--dry-run")
    if args.only:
        argv.extend(["--only", args.only])
    if args.skip:
        argv.extend(["--skip", args.skip])
    raise SystemExit(kb_main(argv))


if __name__ == "__main__":
    main()
