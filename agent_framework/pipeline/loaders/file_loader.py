"""
文件加载器

支持常见格式：TXT / Markdown / CSV / JSON / JSONL /
              PDF / DOCX / XLSX / XLS

每种格式自动提取文本内容和元数据，返回 LoadedDocument 列表。
对于多页文档（PDF）按页拆分；表格文档（CSV/XLSX）按行拆分。

依赖（按需安装）:
    pip install pypdf python-docx openpyxl pandas chardet
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent_framework.pipeline.loaders.base_loader import BaseLoader, LoadedDocument

logger = logging.getLogger(__name__)

# 支持的扩展名 → 处理方法名映射
_EXT_HANDLERS: dict[str, str] = {
    ".txt":   "_load_text",
    ".md":    "_load_text",
    ".rst":   "_load_text",
    ".csv":   "_load_csv",
    ".json":  "_load_json",
    ".jsonl": "_load_jsonl",
    ".pdf":   "_load_pdf",
    ".docx":  "_load_docx",
    ".doc":   "_load_docx",
    ".xlsx":  "_load_excel",
    ".xls":   "_load_excel",
}


class FileLoader(BaseLoader):
    """
    本地文件加载器，根据扩展名自动选择解析方式。

    Example:
        >>> loader = FileLoader()
        >>> docs = loader.load("data/sop_document.docx")
        >>> docs = loader.load_many(["products.csv", "manual.pdf"])
    """

    def __init__(
        self,
        csv_text_columns: list[str] | None = None,
        excel_text_columns: list[str] | None = None,
        json_content_key: str = "content",
        pdf_extract_images: bool = False,
        encoding: str = "utf-8",
    ) -> None:
        """
        Args:
            csv_text_columns:   CSV 中作为文本内容的列名（为空则拼接所有列）
            excel_text_columns: Excel 中作为文本内容的列名（为空则拼接所有列）
            json_content_key:   JSON 对象中存放正文的键名
            pdf_extract_images: 是否尝试提取 PDF 图片中的文字（需 pytesseract）
            encoding:           文本文件默认编码
        """
        self._csv_text_cols = csv_text_columns
        self._excel_text_cols = excel_text_columns
        self._json_content_key = json_content_key
        self._pdf_extract_images = pdf_extract_images
        self._encoding = encoding

    # ── 公共接口 ──────────────────────────────────────────
    def load(self, source: str | Path) -> list[LoadedDocument]:
        """
        根据文件扩展名自动加载。

        Args:
            source: 本地文件路径

        Returns:
            LoadedDocument 列表

        Raises:
            FileNotFoundError: 文件不存在
            ValueError:        不支持的文件格式
        """
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        ext = path.suffix.lower()
        handler_name = _EXT_HANDLERS.get(ext)
        if not handler_name:
            raise ValueError(
                f"不支持的文件格式: {ext}。"
                f"支持: {', '.join(_EXT_HANDLERS)}"
            )

        handler = getattr(self, handler_name)
        docs = handler(path)
        logger.info("已加载 %s：%d 个文档块", path.name, len(docs))
        return docs

    # ── 各格式处理器 ──────────────────────────────────────
    def _base_meta(self, path: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """生成基础元数据。"""
        meta = {
            "source_file": path.name,
            "source_path": str(path.resolve()),
            "file_type": path.suffix.lower().lstrip("."),
        }
        if extra:
            meta.update(extra)
        return meta

    def _detect_encoding(self, path: Path) -> str:
        """使用 chardet 自动检测文件编码，失败则用默认编码。"""
        try:
            import chardet
            raw = path.read_bytes()
            detected = chardet.detect(raw)
            return detected.get("encoding") or self._encoding
        except ImportError:
            return self._encoding

    def _load_text(self, path: Path) -> list[LoadedDocument]:
        """加载纯文本 / Markdown / RST 文件。"""
        enc = self._detect_encoding(path)
        content = path.read_text(encoding=enc, errors="replace")
        return [LoadedDocument(
            content=content,
            metadata=self._base_meta(path),
            source=str(path),
        )]

    def _load_csv(self, path: Path) -> list[LoadedDocument]:
        """加载 CSV，每行转为一条 LoadedDocument。"""
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("请安装: pip install pandas")

        enc = self._detect_encoding(path)
        df = pd.read_csv(path, encoding=enc, dtype=str).fillna("")
        return self._df_to_docs(df, path)

    def _load_excel(self, path: Path) -> list[LoadedDocument]:
        """加载 XLSX / XLS，合并所有 sheet，每行转为一条 LoadedDocument。"""
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("请安装: pip install openpyxl pandas")

        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        docs: list[LoadedDocument] = []
        for sheet_name, df in sheets.items():
            df = df.fillna("")
            extra = {"sheet": sheet_name}
            docs.extend(self._df_to_docs(df, path, extra))
        return docs

    def _df_to_docs(
        self, df, path: Path, extra: dict[str, Any] | None = None
    ) -> list[LoadedDocument]:
        """DataFrame → LoadedDocument 列表（按行）。"""
        text_cols = (
            self._csv_text_cols
            if path.suffix.lower() == ".csv"
            else self._excel_text_cols
        )
        if text_cols:
            valid_cols = [c for c in text_cols if c in df.columns]
        else:
            valid_cols = list(df.columns)

        docs: list[LoadedDocument] = []
        for idx, row in df.iterrows():
            parts = [f"{col}: {row[col]}" for col in valid_cols if str(row[col]).strip()]
            content = "\n".join(parts)
            if not content.strip():
                continue
            meta = self._base_meta(path, extra)
            meta["row_index"] = int(idx)
            # 将所有列写入 metadata，方便过滤
            meta["row_data"] = {c: str(row[c]) for c in df.columns}
            docs.append(LoadedDocument(content=content, metadata=meta, source=str(path)))
        return docs

    def _load_json(self, path: Path) -> list[LoadedDocument]:
        """加载 JSON 文件（对象或数组）。"""
        enc = self._detect_encoding(path)
        raw = json.loads(path.read_text(encoding=enc))
        items = raw if isinstance(raw, list) else [raw]
        return self._json_items_to_docs(items, path)

    def _load_jsonl(self, path: Path) -> list[LoadedDocument]:
        """加载 JSONL 文件（每行一个 JSON 对象）。"""
        enc = self._detect_encoding(path)
        items = [
            json.loads(line)
            for line in path.read_text(encoding=enc).splitlines()
            if line.strip()
        ]
        return self._json_items_to_docs(items, path)

    def _json_items_to_docs(
        self, items: list[Any], path: Path
    ) -> list[LoadedDocument]:
        """JSON 条目列表 → LoadedDocument 列表。"""
        docs: list[LoadedDocument] = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                content = item
                meta = self._base_meta(path, {"item_index": i})
            elif isinstance(item, dict):
                content = item.get(self._json_content_key, "") or " ".join(
                    str(v) for v in item.values() if v
                )
                meta = self._base_meta(path, {"item_index": i, **{
                    k: v for k, v in item.items() if k != self._json_content_key
                }})
            else:
                content = str(item)
                meta = self._base_meta(path, {"item_index": i})

            if content.strip():
                docs.append(LoadedDocument(content=content, metadata=meta, source=str(path)))
        return docs

    def _load_pdf(self, path: Path) -> list[LoadedDocument]:
        """加载 PDF，按页拆分为 LoadedDocument。"""
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError("请安装: pip install pypdf")

        reader = PdfReader(str(path))
        docs: list[LoadedDocument] = []
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                meta = self._base_meta(path, {"page": page_num, "total_pages": len(reader.pages)})
                docs.append(LoadedDocument(
                    content=text,
                    metadata=meta,
                    source=f"{path}#page{page_num}",
                ))
        return docs

    def _load_docx(self, path: Path) -> list[LoadedDocument]:
        """加载 DOCX，提取正文段落和表格内容。"""
        try:
            import docx
        except ImportError:
            raise ImportError("请安装: pip install python-docx")

        doc = docx.Document(str(path))
        parts: list[str] = []

        # 正文段落
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                # 标题段落加前缀，便于分块时识别
                if para.style.name.startswith("Heading"):
                    parts.append(f"## {text}")
                else:
                    parts.append(text)

        # 表格内容
        for table_idx, table in enumerate(doc.tables):
            rows_text: list[str] = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    rows_text.append(" | ".join(cells))
            if rows_text:
                parts.append(f"[表格{table_idx + 1}]\n" + "\n".join(rows_text))

        content = "\n\n".join(parts)
        return [LoadedDocument(
            content=content,
            metadata=self._base_meta(path),
            source=str(path),
        )]
