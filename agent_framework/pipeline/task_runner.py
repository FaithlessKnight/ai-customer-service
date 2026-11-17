"""
任务触发器（Task Runner）

通过简单指令触发暂存区文件的清洗入库任务。

支持的指令:
    run_all()              处理全部 pending 文件
    run_file(file_id)      处理单个文件
    run_by_meta(**kwargs)  按元数据条件过滤后处理
    dry_run(file_id)       预览分块结果，不入库
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent_framework.pipeline.ingest_pipeline import IngestPipeline, IngestResult
from agent_framework.pipeline.staging import StagedFile, StagingArea

logger = logging.getLogger(__name__)


class TaskRunner:
    """
    暂存区任务触发器。

    Example:
        >>> from agent_framework.memory.hybrid_retriever import HybridRetriever
        >>> staging = StagingArea()
        >>> runner = TaskRunner(staging=staging, retriever=HybridRetriever())
        >>>
        >>> # 1. 上传文件
        >>> staging.upload("data/沐浴露.xlsx", metadata={"category": "个护"})
        >>> staging.upload("data/sop退款.docx", metadata={"doc_type": "sop"})
        >>>
        >>> # 2. 查看待处理列表
        >>> staging.print_status()
        >>>
        >>> # 3. 触发清洗（三种方式）
        >>> runner.run_all()                       # 全部处理
        >>> runner.run_file("20240101_沐浴露.xlsx")  # 处理单个
        >>> runner.run_by_meta(category="个护")    # 按类别处理
    """

    def __init__(
        self,
        staging: Optional[StagingArea] = None,
        retriever=None,
        pipeline: Optional[IngestPipeline] = None,
        staging_root: str = None,
    ) -> None:
        if staging_root is None:
            from agent_framework.config.settings import get_settings
            staging_root = get_settings().staging_root
        self.staging = staging or StagingArea(staging_root)

        if pipeline is not None:
            self._pipeline = pipeline
        else:
            if retriever is None:
                from agent_framework.memory.bm25_retriever import BM25Retriever
                retriever = BM25Retriever(source="memory")
                logger.warning("未指定 retriever，使用内存 BM25（仅测试用）")
            self._pipeline = IngestPipeline(retriever=retriever)

    def review_and_ingest(
        self,
        file_path: str,
        metadata: Optional[dict[str, Any]] = None,
        preview_chunks: int = 5,
    ) -> Optional[IngestResult]:
        """
        交互式审核入库流程：上传 -> 清洗预览 -> 人工确认 -> 入库。

        Args:
            file_path:      本地文件路径
            metadata:       附加元数据（doc_type / category 等）
            preview_chunks: 预览展示的最大块数

        Returns:
            IngestResult（确认入库后）或 None（用户取消）

        Example:
            >>> runner.review_and_ingest(
            ...     "data/沐浴露.xlsx",
            ...     metadata={"category": "个护", "doc_type": "product"},
            ... )
        """
        from pathlib import Path

        # 1. 上传到暂存区
        print(f"\n[Step 1] 上传文件到暂存区: {Path(file_path).name}")
        sf = self.staging.upload(file_path, metadata=metadata)
        print(f"  文件 ID : {sf.file_id}")
        print(f"  元数据  : {sf.metadata}")

        # 2. 清洗并展示预览
        print(f"\n[Step 2] 清洗预览（最多展示 {preview_chunks} 块）")
        print("-" * 55)
        chunks = self._pipeline.preview(sf.staged_path, max_chunks=preview_chunks)

        if not chunks:
            print("[预览] 未生成任何块，文件内容可能为空或全被过滤。")
            print("[提示] 文件已保留在暂存区，可用 staging.remove() 删除。")
            return None

        # 获取全量分块用于统计（重置去重后重新预览）
        self._pipeline._cleaner.reset_dedup()
        all_chunks = self._pipeline.preview(sf.staged_path, max_chunks=9999)
        total_chunks = len(all_chunks)
        all_type_count: dict[str, int] = {}
        for c in all_chunks:
            all_type_count[c.chunk_type] = all_type_count.get(c.chunk_type, 0) + 1

        # 重置后重新取前 N 块用于展示
        self._pipeline._cleaner.reset_dedup()
        chunks = self._pipeline.preview(sf.staged_path, max_chunks=preview_chunks)

        print(f"  文件名  : {sf.filename}")
        print(f"  总块数  : {total_chunks}")
        print(f"  块类型  : " + "  ".join(f"{k}:{v}" for k, v in all_type_count.items()))
        print("-" * 55)

        for i, chunk in enumerate(chunks, 1):
            print(f"\n  [块 {i}/{total_chunks}] 类型={chunk.chunk_type}  字符数={len(chunk.content)}")
            preview_text = chunk.content[:300]
            for line in preview_text.splitlines():
                print(f"    {line}")
            if len(chunk.content) > 300:
                print("    ...（内容已截断）")

        if total_chunks > preview_chunks:
            print(f"\n  ... 还有 {total_chunks - preview_chunks} 块未展示")
        print("-" * 55)

        # 3. 人工确认
        print("\n[Step 3] 请确认是否将以上内容入库")
        print("  [y] 确认入库")
        print("  [n] 取消，文件保留在暂存区（可稍后处理）")
        print("  [d] 取消并删除暂存区文件")

        while True:
            choice = input("  请输入选择 (y/n/d): ").strip().lower()
            if choice in ("y", "n", "d"):
                break
            print("  无效输入，请输入 y / n / d")

        if choice == "y":
            # 4. 确认入库
            print("\n[Step 4] 开始入库...")
            result = self._run_one(sf)
            self._print_summary({sf.file_id: result})
            return result
        elif choice == "d":
            self.staging.remove(sf.file_id, delete_file=True)
            print("\n[取消] 文件已从暂存区删除。")
            return None
        else:
            print(f"\n[取消] 文件已保留在暂存区（ID: {sf.file_id}）")
            print("  可稍后使用 runner.run_file(file_id) 入库")
            print("  或使用 staging.remove(file_id, delete_file=True) 删除")
            return None

    # ── 核心指令 ──────────────────────────────────────────
    def run_all(
        self, extra_metadata: Optional[dict[str, Any]] = None
    ) -> dict[str, IngestResult]:
        """
        处理暂存区所有 pending 文件。

        Returns:
            {file_id: IngestResult} 字典
        """
        pending = self.staging.list_files(status="pending")
        if not pending:
            print("[TaskRunner] 暂存区没有待处理文件。")
            return {}

        print(f"[TaskRunner] 开始处理 {len(pending)} 个文件...")
        results: dict[str, IngestResult] = {}
        for sf in pending:
            results[sf.file_id] = self._run_one(sf, extra_metadata=extra_metadata)
        self._print_summary(results)
        return results

    def run_file(
        self,
        file_id: str,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[IngestResult]:
        """
        处理单个指定文件（按 file_id）。
        file_id 由 staging.upload() 返回的 StagedFile.file_id 获取，
        也可通过 staging.list_files() 查看。
        """
        sf = self.staging.get_file(file_id)
        if not sf:
            print(f"[TaskRunner] 文件不存在: {file_id}")
            return None
        if sf.status != "pending":
            print(f"[TaskRunner] 文件状态为 '{sf.status}'，仅 pending 状态可处理。")
            return None
        result = self._run_one(sf, extra_metadata=extra_metadata)
        self._print_summary({file_id: result})
        return result

    def run_by_meta(
        self,
        extra_metadata: Optional[dict[str, Any]] = None,
        **filter_kwargs: Any,
    ) -> dict[str, IngestResult]:
        """
        按元数据条件过滤 pending 文件后处理。

        Args:
            extra_metadata:  附加到所有块的额外元数据
            **filter_kwargs: 元数据过滤条件，支持精确匹配

        Example:
            >>> runner.run_by_meta(category="个护")
            >>> runner.run_by_meta(doc_type="sop", category="售后")
        """
        pending = self.staging.list_files(status="pending")
        matched = [
            sf for sf in pending
            if all(sf.metadata.get(k) == v for k, v in filter_kwargs.items())
        ]

        if not matched:
            print(f"[TaskRunner] 没有符合条件 {filter_kwargs} 的待处理文件。")
            return {}

        print(f"[TaskRunner] 匹配到 {len(matched)} 个文件，开始处理...")
        results: dict[str, IngestResult] = {}
        for sf in matched:
            results[sf.file_id] = self._run_one(sf, extra_metadata=extra_metadata)
        self._print_summary(results)
        return results

    def dry_run(self, file_id: str, max_chunks: int = 5) -> None:
        """
        预览指定文件的清洗和分块结果，不写入数据库。
        适合入库前验证分块效果。

        Args:
            file_id:    暂存区文件 ID
            max_chunks: 最多展示的块数
        """
        sf = self.staging.get_file(file_id)
        if not sf:
            print(f"[TaskRunner] 文件不存在: {file_id}")
            return

        print(f"\n[DryRun] 文件: {sf.filename}  状态: {sf.status}")
        print(f"[DryRun] 元数据: {sf.metadata}")
        print("-" * 50)

        chunks = self._pipeline.preview(sf.staged_path, max_chunks=max_chunks)
        if not chunks:
            print("[DryRun] 未生成任何块（文件内容可能为空或全被过滤）")
            return

        for i, chunk in enumerate(chunks, 1):
            print(f"\n[块 {i}/{len(chunks)}] 类型={chunk.chunk_type}  "
                  f"字符数={len(chunk.content)}")
            print(chunk.content[:200] + ("..." if len(chunk.content) > 200 else ""))
        print("-" * 50)
        print(f"[DryRun] 共预览 {len(chunks)} 块（实际分块可能更多）")

    # ── 内部执行 ──────────────────────────────────────────
    def _get_pipeline_for_collection(self, collection: str) -> IngestPipeline:
        """根据 collection 名称返回对应的 pipeline（动态切换 Milvus collection）。"""
        if collection in (None, "", "default"):
            collection = "products"
        # 如果当前 pipeline 的 retriever 已是该 collection，直接复用
        try:
            vr = self._pipeline._retriever._vector_retriever
            if getattr(vr, 'collection_name', None) == collection:
                return self._pipeline
        except AttributeError:
            pass
        # 否则为该 collection 创建新 pipeline
        from agent_framework.memory.hybrid_retriever import HybridRetriever
        from agent_framework.memory.vector_store import MilvusRetriever
        from agent_framework.memory.bm25_retriever import BM25Retriever
        retriever = HybridRetriever(
            vector_retriever=MilvusRetriever(collection_name=collection),
            bm25_retriever=BM25Retriever(source="postgresql", collection=collection),
        )
        return IngestPipeline(retriever=retriever)

    def _run_one(
        self,
        sf: StagedFile,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> IngestResult:
        """执行单文件清洗入库，自动更新暂存区状态。"""
        print(f"  >> 处理: {sf.filename} ({sf.file_id})")
        # mark_processing 将文件移动到 processing/ 目录，返回含新路径的 StagedFile
        moved = self.staging.mark_processing(sf.file_id)
        actual_path = moved.staged_path if moved else sf.staged_path
        try:
            merged_meta = {**sf.metadata, **(extra_metadata or {})}
            collection = merged_meta.get("collection") or "agent_memory"
            _rep = merged_meta.pop("replace", False)
            replace = _rep is True or str(_rep).lower() in ("true", "1", "on", "yes")
            source_key = merged_meta.get("source_file")
            if replace and source_key:
                from agent_framework.memory.knowledge_delete import delete_chunks_by_source_file

                logger.info(
                    "[replace] 入库前先删除 collection=%s source_file=%r",
                    collection,
                    source_key,
                )
                delete_chunks_by_source_file(collection, str(source_key), dry_run=False)
            pipeline = self._get_pipeline_for_collection(collection)
            result = pipeline.ingest_files(
                file_paths=[actual_path],
                extra_metadata=merged_meta,
            )
            self.staging.mark_done(sf.file_id)
            print(f"  [OK] 完成: {sf.filename} --- {result.summary()}")
        except Exception as e:
            error_msg = str(e)
            self.staging.mark_failed(sf.file_id, error=error_msg)
            result = IngestResult(total_files=1, errors=[error_msg])
            print(f"  [FAIL] 失败: {sf.filename} --- {error_msg}")
            logger.error("处理失败 [%s]: %s", sf.file_id, e)
        return result

    @staticmethod
    def _print_summary(results: dict[str, IngestResult]) -> None:
        """打印批次汇总统计。"""
        total_chunks = sum(r.total_chunks for r in results.values())
        total_written = sum(r.success_count for r in results.values())
        total_errors = sum(len(r.errors) for r in results.values())
        print(
            f"\n[TaskRunner] 批次完成 "
            f"| 文件:{len(results)} "
            f"| 分块:{total_chunks} "
            f"| 写入:{total_written} "
            f"| 错误:{total_errors}"
        )
