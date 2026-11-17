"""
话术 RAG 检索质量验证

用典型客户消息测试 sales_script collection 能否命中正确 chunk。

用法：
    python scripts/verify_rag_quality.py
    python scripts/verify_rag_quality.py --top-k 5
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, ".")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from agent_framework.memory.vector_store import MilvusRetriever
from agent_framework.memory.bm25_retriever import BM25Retriever
from agent_framework.memory.hybrid_retriever import HybridRetriever

# ── 测试用例 ──────────────────────────────────────────────────
# 每条: (描述, 客户消息, 期望命中的 doc_title / content 关键词, 可选 filters)
# 话术库标准文档仅为 data/sales_scripts 下三份「场景一/二/三销售话术.md」
TEST_CASES = [
    (
        "场景一-体验装未用",
        "沐浴乳体验装收到了还没拆封",
        ["场景一", "未使用", "体验装"],
        {"scene": "trial_not_used"},
    ),
    (
        "场景一-太忙忘了",
        "太忙了还没来得及试",
        ["场景一", "忘了", "承接"],
        {"scene": "trial_not_used"},
    ),
    (
        "场景二-使用感受好",
        "用了几次感觉还挺好的，洗完挺清爽",
        ["场景二", "沐浴乳", "回访"],
        {"scene": "trial_used"},
    ),
    (
        "场景二-想买正装",
        "体验装快用完了，正装怎么买",
        ["场景二", "小程序", "买二送一"],
        {"scene": "trial_used"},
    ),
    (
        "场景二-负向肤感",
        "洗完有点干，不太习惯这个香味",
        ["场景二", "负向", "肤感"],
        {"scene": "trial_used"},
    ),
    (
        "场景二-投诉过敏",
        "用了之后皮肤不舒服，有点过敏",
        ["场景二", "抱歉", "停用"],
        {"scene": "trial_used"},
    ),
    (
        "场景三-老客复购",
        "上次买的沐浴乳快用完了，有活动吗",
        ["场景三", "复购", "买二送一"],
        {"scene": "first_order_no_rebuy"},
    ),
    (
        "场景三-价格顾虑",
        "感觉有点贵，别家便宜些",
        ["场景三", "价格", "可悠然"],
        {"scene": "first_order_no_rebuy"},
    ),
]

# CRM 实战风格：包含更口语/上下文摘要化描述，验证检索鲁棒性
CRM_STYLE_CASES = [
    (
        "CRM-场景一-无回复触达",
        "客户领了体验装但一直没开封，也没回消息，帮我发第一轮提醒",
        ["场景一", "第1轮", "提醒使用"],
        {"scene": "trial_not_used"},
    ),
    (
        "CRM-场景二-成分关注",
        "客户反馈想看成分和使用感受，之前试用过了",
        ["场景二", "首次体验回访", "感受"],
        {"scene": "trial_used"},
    ),
    (
        "CRM-场景二-投诉风险",
        "客户说用后皮肤不舒服，情绪比较差，先给安抚和处理建议",
        ["场景二", "抱歉", "停用"],
        {"scene": "trial_used"},
    ),
    (
        "CRM-场景三-复购推进",
        "首单后两个月，客户说快用完了，想要活动信息",
        ["场景三", "买二送一", "复购"],
        {"scene": "first_order_no_rebuy"},
    ),
]


def check_hit(chunks, keywords: list[str]) -> tuple[bool, str]:
    """检查结果里是否有 chunk 包含任一关键词（title 或 content）。"""
    for c in chunks:
        title = c.metadata.get("doc_title", "") + c.metadata.get("section", "")
        content = c.content
        combined = title + content
        for kw in keywords:
            if kw in combined:
                return True, kw
    return False, ""


def run_suite(name: str, cases, retriever, top_k: int, no_filter: bool = False) -> tuple[int, int]:
    print(f"\n=== {name} ===")
    passed = 0
    failed = 0

    for desc, query, keywords, filters in cases:
        f = None if no_filter else filters
        chunks = retriever.retrieve(query, k=top_k, filters=f)
        hit, matched_kw = check_hit(chunks, keywords)

        status = "✓" if hit else "✗"
        if hit:
            passed += 1
        else:
            failed += 1

        print(f"[{status}] {desc}")
        print(f"    查询: {query[:50]}")
        if hit:
            print(f"    命中关键词: {matched_kw}")
        else:
            print(f"    期望关键词: {keywords}")
        print(f"    Top-{top_k} 结果:")
        for i, c in enumerate(chunks[:top_k], 1):
            t = c.metadata.get("doc_title", "?")
            st = c.metadata.get("section", "")
            sc = c.metadata.get("scene", "")
            score = getattr(c, "score", None)
            score_str = f" score={score:.3f}" if score else ""
            print(f"      {i}. [{sc}] {t} > {st}{score_str}")
        print()

    total = passed + failed
    print(f"{name} 结果: {passed}/{total} 通过  {'✓' if failed == 0 else f'{failed} 条未命中'}")
    return passed, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--no-filter", action="store_true", help="忽略 scene filter")
    args = parser.parse_args()

    print("初始化检索器…")
    vec = MilvusRetriever(collection_name="sales_script")
    bm25 = BM25Retriever(collection="sales_script")
    retriever = HybridRetriever(vector_retriever=vec, bm25_retriever=bm25)
    print("OK\n")

    p1, f1 = run_suite("基础场景集", TEST_CASES, retriever, args.top_k, args.no_filter)
    p2, f2 = run_suite("CRM风格集", CRM_STYLE_CASES, retriever, args.top_k, args.no_filter)
    total_p = p1 + p2
    total_f = f1 + f2
    total = total_p + total_f
    print(f"\n总结果: {total_p}/{total} 通过  {'✓' if total_f == 0 else f'{total_f} 条未命中'}")


if __name__ == "__main__":
    main()
