"""从桌面读取三份 TXT，写入仓库 `data/sales_scripts/*.md`（含 scene / journey_stage），供 ingest_sales_scripts 入库。"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "data" / "sales_scripts"
DESKTOP = Path(r"C:\Users\1\Desktop")

# 入库元数据仅保留 scene + journey_stage，与 CRM / API 一致；详见 data/sales_scripts/README_话术文档规范.md
SPECS: list[tuple[str, str, str, dict[str, str]]] = [
    (
        "场景一销售话术.txt",
        "场景一销售话术.md",
        "场景一销售话术（领取体验装后未使用）",
        {"scene": "trial_not_used", "journey_stage": "first_outreach"},
    ),
    (
        "场景二销售话术.txt",
        "场景二销售话术.md",
        "场景二销售话术（领取体验装后已使用）",
        {"scene": "trial_used", "journey_stage": "feedback_collecting"},
    ),
    (
        "场景三销售话术.txt",
        "场景三销售话术.md",
        "场景三销售话术（首单后约2个月未复购）",
        {"scene": "first_order_no_rebuy", "journey_stage": "post_order_care"},
    ),
]


def _table(meta: dict[str, str]) -> str:
    rows = ["| 字段 | 值 |", "|------|-----|"]
    for k, v in meta.items():
        rows.append(f"| {k} | {v} |")
    return "\n".join(rows)


def main() -> None:
    for txt_name, md_name, h1_title, meta in SPECS:
        src = DESKTOP / txt_name
        if not src.exists():
            raise SystemExit(f"找不到源文件: {src}")
        body = src.read_text(encoding="utf-8").strip()
        out = OUT_DIR / md_name
        md = "\n".join(
            [
                f"# {h1_title}",
                "",
                "> 适用场景标签仅含 `scene` + `journey_stage`（与 CRM 一致）。入库：`python scripts/ingest_sales_scripts.py --source data/sales_scripts --dry-run`",
                "",
                "## 一、适用场景标签",
                "",
                _table(meta),
                "",
                "---",
                "",
                "## 二、话术正文",
                "",
                body,
                "",
            ]
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"写入 {out} ({len(md)} 字符)")


if __name__ == "__main__":
    main()
