from __future__ import annotations

from pathlib import Path

# 与 api/routers/ingest.py 白名单对齐（客服常用子集）
VALID_COLLECTIONS: frozenset[str] = frozenset(
    {
        "products",
        "faq",
        "sop",
        "policy",
        "agent_memory",
        "moments_knowledge",
        "sales_script",
        "palm_diagnosis",
    }
)


def infer_collection(path: Path, explicit: str | None) -> str:
    """
    未指定 --collection 时根据路径推断。
    explicit 非空时原样返回（需调用方校验 VALID）。
    """
    if explicit and explicit.strip():
        return explicit.strip()

    parts = {p.lower() for p in path.parts}
    s = str(path).replace("\\", "/").lower()

    if "data/sales_scripts" in s or path.name.endswith("销售话术.md"):
        return "sales_script"
    if "product" in parts or "/product/" in s or "\\product\\" in s:
        return "products"
    if "sales_scripts" in parts:
        return "sales_script"

    raise ValueError(
        "无法从路径推断 collection，请显式指定 --collection，"
        f"可选: {', '.join(sorted(VALID_COLLECTIONS))}"
    )
