"""任务产物的目录契约与原子读写（无数据库，JSON 落盘）。

目录结构：
data/
├─ tasks/<task_id>/
│   ├─ <upload>.<ext>          上传原件
│   ├─ page_001.png            页面图片（PDF/图片）
│   ├─ pages.json              解析后的页面文本
│   ├─ page_001.md             VLM 转写结果（按需）
│   ├─ segments.json           主单/分单/舱单分段结果
│   ├─ house_bills.json        分单抽取结果
│   ├─ manifest.json           舱单抽取结果
│   ├─ comparisons.json        分单 vs 舱单核对结果
│   ├─ task_status.json        任务快照
│   └─ process.log             JSONL 事件流（SSE 回放源）
└─ results/<task_id>.json      最终结果
"""

import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.schemas.file import UploadedDocument

logger = logging.getLogger(__name__)


class FileStore:
    DIRECTORY_NAMES = ("tasks", "results")

    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.data_root).resolve()
        self.ensure_directories()

    def ensure_directories(self) -> None:
        for name in self.DIRECTORY_NAMES:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def new_task_id(self, source_filename: str | None = None) -> str:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        label = ""
        if source_filename:
            label = f"_{Path(self.safe_filename(source_filename)).stem}"
        return f"task_{timestamp}{label[:40]}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def safe_filename(filename: str) -> str:
        name = Path(filename).name
        stem = Path(name).stem
        suffix = Path(name).suffix.lower()
        safe_stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).strip("._") or "document"
        return f"{safe_stem[:120]}{suffix}"

    def task_dir(self, task_id: str) -> Path:
        directory = self.root / "tasks" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_upload(self, task_id: str, upload: UploadedDocument) -> Path:
        filename = self.safe_filename(upload.filename or "document")
        destination = self.task_dir(task_id) / filename
        self.write_bytes_atomic(destination, upload.content)
        return destination

    def write_bytes_atomic(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)

    def write_text_atomic(self, destination: Path, content: str) -> None:
        self.write_bytes_atomic(destination, content.encode("utf-8"))

    def append_line(self, destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as file:
            file.write(content + "\n")

    def write_json_atomic(self, destination: Path, payload: Any) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        self.write_text_atomic(destination, content)

    def read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_json_or_none(self, path: Path) -> Any:
        try:
            return self.read_json(path)
        except (OSError, ValueError):
            return None

    def read_text(self, path: Path, errors: str = "strict") -> str:
        return path.read_text(encoding="utf-8", errors=errors)

    def relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def resolve_relative(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise ValueError("INVALID_PATH")
        return candidate

    # ---- 任务内固定产物 ----
    def task_status_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "task_status.json"

    def process_log_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "process.log"

    def document_path(self, task_id: str) -> Path:
        """页面元数据 + 分段 + 主单登记（转写全文在 transcription/*.md）。"""

        return self.task_dir(task_id) / "document.json"

    def documents_path(self, task_id: str) -> Path:
        """最终抽取结果：分单字段/定位 + 舱单（不含候选）。"""

        return self.task_dir(task_id) / "documents.json"

    def candidates_path(self, task_id: str) -> Path:
        """字段候选唯一来源：逐字段 value/status/confidence/evidence。"""

        return self.task_dir(task_id) / "candidates.json"

    def comparisons_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "comparisons.json"

    def page_image_path(self, task_id: str, index: int) -> Path:
        return self.task_dir(task_id) / f"page_{index:03d}.png"

    # ---- VLM 转写产物（可与提取结果逐字段对照） ----
    def transcription_dir(self, task_id: str) -> Path:
        directory = self.task_dir(task_id) / "transcription"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def transcription_path(self, task_id: str, index: int, suffix: str = "") -> Path:
        """整页转写 page_001.md；分单局部精读 page_001_zoom.md。"""

        return self.transcription_dir(task_id) / f"page_{index:03d}{suffix}.md"

    def zoom_status_path(self, task_id: str) -> Path:
        """分单局部精读的完成记录（含跳过/失败页，避免断点续跑无限重试）。"""

        return self.transcription_dir(task_id) / "zoom.json"

    def result_path(self, task_id: str) -> Path:
        return self.root / "results" / f"{task_id}.json"

    def submission_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "submission.json"

    def cleanup_expired(self, retention_hours: float) -> int:
        """删除超过保留期的任务目录与结果文件；retention_hours<=0 表示永久保留。"""

        if retention_hours <= 0:
            return 0
        cutoff = time.time() - retention_hours * 3600
        removed = 0
        for task_dir in (self.root / "tasks").iterdir():
            if not task_dir.is_dir():
                continue
            status = self.read_json_or_none(task_dir / "task_status.json") or {}
            if isinstance(status, dict) and status.get("status") == "running":
                continue
            try:
                expired = task_dir.stat().st_mtime < cutoff
            except OSError:
                continue
            if expired:
                shutil.rmtree(task_dir, ignore_errors=True)
                removed += 1
        for result in (self.root / "results").iterdir():
            if not result.is_file():
                continue
            try:
                expired = result.stat().st_mtime < cutoff
            except OSError:
                continue
            if expired:
                result.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info("已清理 %s 个过期任务产物", removed)
        return removed
