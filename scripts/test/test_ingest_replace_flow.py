"""
离线验证：暂存入库在 replace=true 时会先按 collection + source_file 删除，再 ingest。
不连接 Milvus/PG（删除与 collection 专用 pipeline 均 mock）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_framework.pipeline.ingest_pipeline import IngestResult
from agent_framework.pipeline.staging import StagingArea
from agent_framework.pipeline.task_runner import TaskRunner


@pytest.fixture()
def tmp_staging():
    root = tempfile.mkdtemp()
    yield root


def _write_txt(root: str, name: str, body: str) -> Path:
    p = Path(root) / name
    p.write_text(body, encoding="utf-8")
    return p


def test_run_one_replace_true_calls_delete_before_ingest(tmp_staging):
    src = _write_txt(tmp_staging, "doc.txt", "chunk body one")
    staging = StagingArea(staging_root=tmp_staging)
    sf = staging.upload(
        str(src),
        metadata={
            "collection": "faq",
            "source_file": "doc.txt",
            "replace": True,
        },
    )
    runner = TaskRunner(staging=staging, pipeline=MagicMock())
    mock_ingest_pipeline = MagicMock()
    mock_ingest_pipeline.ingest_files.return_value = IngestResult(
        total_files=1,
        total_docs=1,
        cleaned_docs=1,
        total_chunks=2,
        ingested_ids=["a", "b"],
    )

    with patch(
        "agent_framework.memory.knowledge_delete.delete_chunks_by_source_file"
    ) as del_mock, patch.object(
        runner, "_get_pipeline_for_collection", return_value=mock_ingest_pipeline
    ):
        result = runner._run_one(staging.get_file(sf.file_id))

    del_mock.assert_called_once_with("faq", "doc.txt", dry_run=False)
    mock_ingest_pipeline.ingest_files.assert_called_once()
    call_kw = mock_ingest_pipeline.ingest_files.call_args
    assert "extra_metadata" in call_kw.kwargs
    assert "replace" not in call_kw.kwargs["extra_metadata"]
    assert call_kw.kwargs["extra_metadata"].get("source_file") == "doc.txt"
    assert result.success_count == 2


@pytest.mark.parametrize(
    "replace_val,expect_delete",
    [
        (True, True),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("on", True),
        (False, False),
        ("false", False),
        ("", False),
    ],
)
def test_run_one_replace_variants(tmp_staging, replace_val, expect_delete):
    sub = Path(tmp_staging) / "st"
    sub.mkdir()
    src = _write_txt(str(sub), "x.txt", "x")
    staging = StagingArea(staging_root=str(sub))
    sf = staging.upload(
        str(src),
        metadata={
            "collection": "products",
            "source_file": "x.txt",
            "replace": replace_val,
        },
    )
    runner = TaskRunner(staging=staging, pipeline=MagicMock())
    mock_pipe = MagicMock()
    mock_pipe.ingest_files.return_value = IngestResult(
        total_files=1, ingested_ids=["1"]
    )

    with patch(
        "agent_framework.memory.knowledge_delete.delete_chunks_by_source_file"
    ) as del_mock, patch.object(
        runner, "_get_pipeline_for_collection", return_value=mock_pipe
    ):
        runner._run_one(staging.get_file(sf.file_id))

    if expect_delete:
        del_mock.assert_called_once()
    else:
        del_mock.assert_not_called()


def test_run_one_replace_true_but_no_source_file_skips_delete(tmp_staging):
    sub = Path(tmp_staging) / "st2"
    sub.mkdir()
    src = _write_txt(str(sub), "y.txt", "y")
    staging = StagingArea(staging_root=str(sub))
    sf = staging.upload(
        str(src),
        metadata={"collection": "faq", "replace": True},
    )
    runner = TaskRunner(staging=staging, pipeline=MagicMock())
    mock_pipe = MagicMock()
    mock_pipe.ingest_files.return_value = IngestResult(
        total_files=1, ingested_ids=["1"]
    )

    with patch(
        "agent_framework.memory.knowledge_delete.delete_chunks_by_source_file"
    ) as del_mock, patch.object(
        runner, "_get_pipeline_for_collection", return_value=mock_pipe
    ):
        runner._run_one(staging.get_file(sf.file_id))

    del_mock.assert_not_called()


def test_upload_replace_string_parsing():
    """与 api/routers/ingest.py 中 replace 解析保持一致。"""
    for s, want in [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("false", False),
        ("", False),
    ]:
        got = str(s).lower() in ("true", "1", "on", "yes")
        assert got == want, (s, want, got)
