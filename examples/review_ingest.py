"""
示例 ：交互式文件审核入库

流程：上传文件 -> 清洗预览 -> 人工确认 -> 入库

运行方式:
    python examples/review_ingest.py
    python examples/review_ingest.py data/uploads/your_file.xlsx
"""

import sys
from pathlib import Path

from agent_framework.pipeline import StagingArea, TaskRunner
from agent_framework.memory.bm25_retriever import BM25Retriever

# 项目根目录（无论从哪里运行都能正确定位）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def main():
    # ── 从命令行参数获取文件路径，默认用 README.md 演示 ──
    if len(sys.argv) > 1:
        file_path = Path(sys.argv[1])
        if not file_path.is_absolute():
            file_path = PROJECT_ROOT / file_path
    else:
        file_path = PROJECT_ROOT / "README.md"
        print(f"[提示] 未指定文件，使用默认演示文件: {file_path.name}")
        print(f"[提示] 用法: python examples/review_ingest.py data/uploads/your_file.xlsx\n")

    if not file_path.exists():
        print(f"[错误] 文件不存在: {file_path}")
        sys.exit(1)

    # ── 初始化暂存区和任务触发器 ─────────────────────────
    staging = StagingArea(staging_root=str(PROJECT_ROOT / "data" / "staging"))

    # 开发测试用内存 BM25，上线替换为:
    #   from agent_framework.memory.hybrid_retriever import HybridRetriever
    #   runner = TaskRunner(staging=staging, retriever=HybridRetriever())
    runner = TaskRunner(staging=staging, retriever=BM25Retriever(source="memory"))

    # ── 交互式审核入库 ────────────────────────────────────
    result = runner.review_and_ingest(
        file_path=str(file_path),
        metadata={
            "doc_type": "product",    # 文档类型：product / sop / faq
            "category": "个护清洁",   # 业务分类
        },
        preview_chunks=5,             # 预览展示前 5 块
    )

    # ── 入库后检索验证 ────────────────────────────────────
    if result and result.success_count > 0:
        print("\n" + "=" * 55)
        print("检索验证")
        print("=" * 55)
        retriever = runner._pipeline._retriever
        queries = ["退货政策", "商品规格", "售后服务"]
        for q in queries:
            hits = retriever.retrieve(q, k=1)
            if hits:
                print(f"  查询: {q!r}")
                print(f"  匹配: {hits[0].content[:80]}...\n")


if __name__ == "__main__":
    main()
