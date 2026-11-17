"""
文件暂存区管理模块

提供文件上传到本地暂存区、查看待处理文件、移除文件等功能。
暂存区是一个本地目录，文件上传后处于「等待清洗」状态，
不会自动入库，直到通过 TaskRunner 发出指令才开始处理。

暂存区目录结构:
    data/staging/
    ├── pending/     ← 上传后等待处理
    ├── processing/  ← 正在清洗中
    ├── done/        ← 已完成入库
    └── failed/      ← 处理失败
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

FileStatus = Literal["pending", "processing", "done", "failed"]

SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".rst",
    ".csv", ".json", ".jsonl",
    ".pdf", ".docx", ".doc",
    ".xlsx", ".xls",
}


@dataclass
class StagedFile:
    """
    暂存文件的元信息。

    Attributes:
        file_id:      唯一标识（时间戳_文件名）
        filename:     原始文件名
        status:       当前状态
        staged_path:  暂存区内的绝对路径
        upload_time:  上传时间
        metadata:     用户附加的元数据（文档类型、业务分类等）
        error:        失败原因（失败时填写）
    """
    file_id: str
    filename: str
    status: FileStatus
    staged_path: str
    upload_time: str
    metadata: dict = field(default_factory=dict)
    error: str = ""


class StagingArea:
    """
    文件暂存区。

    Example:
        >>> staging = StagingArea()
        >>> staging.upload("data/沐浴露商品.xlsx", metadata={"category": "个护"})
        >>> staging.list_files()          # 查看所有待处理文件
        >>> staging.list_files("pending") # 仅查看等待处理的文件
    """

    def __init__(self, staging_root: str = None) -> None:
        if staging_root is None:
            from agent_framework.config.settings import get_settings
            staging_root = get_settings().staging_root
        self._root = Path(staging_root)
        self._pending   = self._root / "pending"
        self._processing = self._root / "processing"
        self._done      = self._root / "done"
        self._failed    = self._root / "failed"
        self._meta_file = self._root / "manifest.json"

        # 确保目录存在
        for d in (self._pending, self._processing, self._done, self._failed):
            d.mkdir(parents=True, exist_ok=True)

        self._manifest: dict[str, dict] = self._load_manifest()

    # ── 上传 ───────────────────────────────────────────────
    def upload(
        self,
        file_path: str | Path,
        metadata: Optional[dict] = None,
        overwrite: bool = False,
    ) -> StagedFile:
        """
        将文件复制到暂存区 pending 目录，状态置为 'pending'。

        Args:
            file_path:  本地文件路径（源文件不会被删除）
            metadata:   附加元数据（category / doc_type / brand 等）
            overwrite:  同名文件已存在时是否覆盖

        Returns:
            StagedFile 对象

        Raises:
            FileNotFoundError: 源文件不存在
            ValueError:        不支持的文件格式
        """
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"文件不存在: {src}")
        if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"不支持的格式: {src.suffix}。支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
        file_id = f"{ts}_{src.name}"
        dest = self._pending / file_id

        if dest.exists() and not overwrite:
            logger.warning("文件已在暂存区: %s（使用 overwrite=True 覆盖）", file_id)
        else:
            shutil.copy2(src, dest)
            logger.info("文件已上传到暂存区: %s", file_id)

        staged = StagedFile(
            file_id=file_id,
            filename=src.name,
            status="pending",
            staged_path=str(dest),
            upload_time=datetime.now().isoformat(),
            metadata=metadata or {},
        )
        self._manifest[file_id] = asdict(staged)
        self._save_manifest()
        return staged

    def upload_many(
        self,
        file_paths: list[str | Path],
        metadata: Optional[dict] = None,
    ) -> list[StagedFile]:
        """批量上传多个文件（共享同一份 metadata）。"""
        results = []
        for fp in file_paths:
            try:
                results.append(self.upload(fp, metadata=metadata))
            except Exception as e:
                logger.error("上传失败 [%s]: %s", fp, e)
        return results

    # ── 查看 ───────────────────────────────────────────────
    def list_files(
        self, status: Optional[FileStatus] = None
    ) -> list[StagedFile]:
        """
        列出暂存区文件。

        Args:
            status: 过滤状态（None 表示全部）

        Returns:
            StagedFile 列表，按上传时间升序
        """
        files = [
            StagedFile(**v)
            for v in self._manifest.values()
            if status is None or v["status"] == status
        ]
        return sorted(files, key=lambda f: f.upload_time)

    def get_file(self, file_id: str) -> Optional[StagedFile]:
        """按 file_id 获取文件信息。"""
        data = self._manifest.get(file_id)
        return StagedFile(**data) if data else None

    def print_status(self) -> None:
        """在终端打印暂存区状态概览。"""
        all_files = self.list_files()
        pending    = [f for f in all_files if f.status == "pending"]
        processing = [f for f in all_files if f.status == "processing"]
        done       = [f for f in all_files if f.status == "done"]
        failed     = [f for f in all_files if f.status == "failed"]

        print("\n" + "=" * 55)
        print(f"  暂存区状态  |  待处理:{len(pending)}  处理中:{len(processing)}  "
              f"已完成:{len(done)}  失败:{len(failed)}")
        print("=" * 55)
        for f in all_files:
            icon = {"pending": "[pending]", "processing": "[process]", "done": "[done]   ", "failed": "[failed] "}.get(f.status, "[?]     ")
            print(f"  {icon} {f.filename}  ({f.file_id})")
            if f.metadata:
                print(f"             元数据: {f.metadata}")
            if f.error:
                print(f"             错误: {f.error}")
        print("=" * 55 + "\n")

    # ── 状态变更 ───────────────────────────────────────────
    def mark_processing(self, file_id: str) -> Optional[StagedFile]:
        """将文件状态改为 processing，并移动到 processing 目录。"""
        return self._move(file_id, "processing", self._processing)

    def mark_done(self, file_id: str) -> Optional[StagedFile]:
        """将文件状态改为 done，并移动到 done 目录。"""
        return self._move(file_id, "done", self._done)

    def mark_failed(self, file_id: str, error: str = "") -> Optional[StagedFile]:
        """将文件状态改为 failed，并移动到 failed 目录。"""
        staged = self._move(file_id, "failed", self._failed)
        if staged and error:
            self._manifest[file_id]["error"] = error
            self._save_manifest()
        return staged

    def remove(self, file_id: str, delete_file: bool = False) -> bool:
        """
        从暂存区移除文件记录。

        Args:
            file_id:     文件 ID
            delete_file: 是否同时删除磁盘文件
        """
        info = self._manifest.pop(file_id, None)
        if info and delete_file:
            p = Path(info["staged_path"])
            if p.exists():
                p.unlink()
        self._save_manifest()
        return info is not None

    # ── 内部工具 ───────────────────────────────────────────
    def _move(
        self, file_id: str, new_status: FileStatus, target_dir: Path
    ) -> Optional[StagedFile]:
        info = self._manifest.get(file_id)
        if not info:
            logger.warning("file_id 不存在: %s", file_id)
            return None
        src = Path(info["staged_path"])
        dest = target_dir / src.name
        if src.exists():
            shutil.move(str(src), dest)
        info["status"] = new_status
        info["staged_path"] = str(dest)
        self._manifest[file_id] = info
        self._save_manifest()
        return StagedFile(**info)

    def _load_manifest(self) -> dict:
        if self._meta_file.exists():
            try:
                return json.loads(self._meta_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_manifest(self) -> None:
        self._meta_file.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
