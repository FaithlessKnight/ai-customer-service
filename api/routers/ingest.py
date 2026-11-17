"""
数据入库接口路由

提供：
  POST /ingest/texts              直接传入文本入库
  POST /ingest/upload             上传文件到暂存区（可选 replace：入库前按 source_file 删旧块）
  GET  /ingest/staging            查看暂存区状态
  POST /ingest/staging/run        触发暂存区文件入库
  DELETE /ingest/staging/{id}     删除暂存区文件
  DELETE /ingest/document         按 collection + source_file 删除已入库块（PG+Milvus）
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from api.dependencies import PipelineDep, StagingDep, TaskRunnerDep

# 允许的知识库集合名
_VALID_COLLECTIONS = {"products", "faq", "sop", "policy", "agent_memory", "moments_knowledge", "sales_script", "palm_diagnosis"}
from api.schemas import (
    IngestResponse,
    IngestTextRequest,
    OKResponse,
    StagingRunRequest,
    StagingStatusResponse,
    StagingUploadResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["数据入库"])


@router.post("/texts", response_model=IngestResponse, summary="直接传入文本入库")
async def ingest_texts(
    request: IngestTextRequest,
    pipeline: PipelineDep,
) -> IngestResponse:
    """
    将文本列表直接清洗分块后入库。
    适合从已有系统接口获取数据后传入。
    """
    try:
        result = pipeline.ingest_texts(
            texts=request.texts,
            metadatas=request.metadatas,
            extra_metadata={"collection": request.collection},
        )
        return IngestResponse(
            total_chunks=result.total_chunks,
            ingested=result.success_count,
            skipped=result.skipped,
            errors=result.errors,
            summary=result.summary(),
        )
    except Exception as e:
        logger.error("文本入库失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload", response_model=StagingUploadResponse, summary="上传文件到暂存区")
async def upload_file(
    file: UploadFile = File(..., description="上传的文件"),
    doc_type: str = Form(default="product", description="文档类型：product / sop / faq"),
    category: str = Form(default="", description="业务分类"),
    collection: str = Form(default="default", description="知识库集合名"),
    replace: str = Form(
        default="false",
        description="是否入库前按 source_file 删除旧块：true / false",
    ),
    staging: StagingDep = None,
) -> StagingUploadResponse:
    """
    上传文件到暂存区（pending 状态），不立即入库。
    支持格式：txt / md / csv / json / jsonl / pdf / docx / xlsx / xls

    元数据中的 ``source_file`` 固定为清理后的原始文件名，入库时会写入各 chunk，
    便于与 ``DELETE /ingest/document`` 或 ``replace=true`` 对齐。
    """
    # 检查文件格式
    from agent_framework.pipeline.staging import SUPPORTED_EXTENSIONS
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {suffix}。支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    # 验证 collection 名称
    if collection not in _VALID_COLLECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的知识库集合: {collection!r}，可用集合: {sorted(_VALID_COLLECTIONS)}",
        )

    safe_filename = Path(file.filename or "unnamed").name
    safe_filename = re.sub(r'[<>:"|?*\x00-\x1f]', '_', safe_filename)[:200]
    replace_bool = str(replace).lower() in ("true", "1", "on", "yes")

    # 保存到临时文件
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix="upload_"
        ) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # 上传到暂存区
        metadata = {
            "doc_type": doc_type,
            "collection": collection,
            "source_file": safe_filename,
            "replace": replace_bool,
        }
        if category:
            metadata["category"] = category
        metadata["original_filename"] = file.filename

        staged = staging.upload(tmp_path, metadata=metadata)
        # 将暂存文件重命名为原始文件名
        # 清理文件名：去掉目录分隔符和特殊字符，防止路径穿越
        new_path = Path(staged.staged_path).parent / f"{staged.file_id[:19]}_{safe_filename}"
        Path(staged.staged_path).rename(new_path)
        staging._manifest[staged.file_id]["staged_path"] = str(new_path)
        staging._manifest[staged.file_id]["filename"] = safe_filename
        staging._save_manifest()

        return StagingUploadResponse(
            file_id=staged.file_id,
            filename=file.filename or "",
            status="pending",
            message=f"文件已上传到暂存区，使用 POST /ingest/staging/run 触发入库",
        )
    except Exception as e:
        logger.error("文件上传失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


@router.get("/staging", response_model=StagingStatusResponse, summary="查看暂存区状态")
async def get_staging_status(staging: StagingDep) -> StagingStatusResponse:
    """查看暂存区所有文件及状态。"""
    all_files = staging.list_files()
    return StagingStatusResponse(
        pending=sum(1 for f in all_files if f.status == "pending"),
        processing=sum(1 for f in all_files if f.status == "processing"),
        done=sum(1 for f in all_files if f.status == "done"),
        failed=sum(1 for f in all_files if f.status == "failed"),
        files=[
            {
                "file_id": f.file_id,
                "filename": f.filename,
                "status": f.status,
                "upload_time": f.upload_time,
                "metadata": f.metadata,
                "error": f.error,
            }
            for f in all_files
        ],
    )


@router.post("/staging/run", response_model=IngestResponse, summary="触发暂存区文件入库")
async def run_staging(
    request: StagingRunRequest,
    runner: TaskRunnerDep,
) -> IngestResponse:
    """
    触发暂存区文件的清洗入库。

    - 不指定 file_id 和 filter_meta：处理全部 pending 文件
    - 指定 file_id：处理单个文件
    - 指定 filter_meta：按元数据过滤后处理
    """
    try:
        if request.file_id:
            results = {}
            r = runner.run_file(request.file_id)
            if r:
                results[request.file_id] = r
        elif request.filter_meta:
            results = runner.run_by_meta(**request.filter_meta)
        else:
            results = runner.run_all()

        total_chunks = sum(r.total_chunks for r in results.values())
        total_ingested = sum(r.success_count for r in results.values())
        total_skipped = sum(r.skipped for r in results.values())
        all_errors = [e for r in results.values() for e in r.errors]

        return IngestResponse(
            total_chunks=total_chunks,
            ingested=total_ingested,
            skipped=total_skipped,
            errors=all_errors,
            summary=f"处理文件:{len(results)} | 分块:{total_chunks} | 写入:{total_ingested}",
        )
    except Exception as e:
        logger.error("暂存区入库失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/staging/{file_id}", response_model=OKResponse, summary="删除暂存区文件")
async def delete_staging_file(
    file_id: str,
    delete_file: bool = True,
    staging: StagingDep = None,
) -> OKResponse:
    """从暂存区删除指定文件。"""
    removed = staging.remove(file_id, delete_file=delete_file)
    if not removed:
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")
    return OKResponse(message=f"已删除: {file_id}")


@router.delete("/document", response_model=OKResponse, summary="按文件名删除已入库知识块")
async def delete_ingested_by_source_file(
    collection: str = Query(..., description="collection 名，如 products"),
    source_file: str = Query(..., description="与写入 metadata.source_file 一致，通常为上传时的原始文件名"),
) -> OKResponse:
    """
    删除 PostgreSQL + Milvus 中 ``collection`` 且 ``metadata.source_file == source_file`` 的块。
    与 ``python scripts/kb.py delete --collection … --source …`` 行为一致。
    """
    if collection not in _VALID_COLLECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的知识库集合: {collection!r}，可用: {sorted(_VALID_COLLECTIONS)}",
        )
    if not source_file.strip():
        raise HTTPException(status_code=400, detail="source_file 不能为空")
    try:
        from agent_framework.memory.knowledge_delete import delete_chunks_by_source_file

        delete_chunks_by_source_file(collection, source_file.strip(), dry_run=False)
    except Exception as e:
        logger.error("删除已入库文档失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    return OKResponse(
        message=f"已删除 collection={collection} source_file={source_file!r}（PG+Milvus）",
    )
