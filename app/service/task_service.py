"""任务生命周期：上传校验、异步执行、进度事件、产物查询与重试。

单进程内用 asyncio 调度（与 DocMind 一致）：部署时只能起 1 个 uvicorn worker，
否则任务状态文件会被多进程同时读写。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.agent.gateway import build_gateway
from app.config import Settings
from app.schemas.documents import TaskSnapshot, WorkflowEvent
from app.schemas.file import UploadedDocument
from app.schemas.submit import SubmitRequest, SubmitResponse
from app.service.gates import ConcurrencyGates
from app.service.pipeline import Pipeline, PipelineError
from app.storage.file_store import FileStore
from app.storage.task_docs import load_document, load_documents, load_pages_meta
from app.workflow.errors import TaskCancelledError, TaskPausedError

if TYPE_CHECKING:
    from app.workflow.graph import AnalysisGraph

logger = logging.getLogger(__name__)

# 终态：进入后不再被节点事件或中断请求改写
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
# 冻结态 = 终态 + 暂停：事件只追加日志，不改写状态
FROZEN_STATUSES = TERMINAL_STATUSES | {"paused"}


class TaskError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class TaskNotFoundError(TaskError):
    def __init__(self, task_id: str) -> None:
        super().__init__("TASK_NOT_FOUND", f"任务不存在：{task_id}", http_status=404)


class TaskReporter:
    """节点进度回调（注入 LangGraph state，不落盘）。"""

    def __init__(self, service: TaskService, task_id: str) -> None:
        self.service = service
        self.task_id = task_id

    async def emit(
        self,
        step: str,
        message: str,
        progress: int,
        level: str = "info",
        *,
        phase: str = "",
        duration_ms: int | None = None,
    ) -> None:
        await self.service.emit_event(
            self.task_id,
            step,
            message,
            progress,
            level,  # type: ignore[arg-type]
            phase=phase,  # type: ignore[arg-type]
            duration_ms=duration_ms,
        )

    async def checkpoint(self) -> None:
        """节点入口的中断检查：命中暂停/取消时抛出对应异常。"""

        await self.service.checkpoint(self.task_id)


class TaskService:
    def __init__(self, settings: Settings, gateway: Any = None) -> None:
        self.settings = settings
        self.store = FileStore(settings)
        self._running: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        # 进程内并发闸门：任务 / 模型 / Office 转 PDF 共用一套，便于统一观测排队
        self.gates = ConcurrencyGates()
        self.gateway: Any = None
        self.pipeline: Pipeline | None = None
        self.graph: AnalysisGraph | None = None
        self._gateway_error: str | None = None
        # 注入网关用于测试（离线跑管线）；生产走真实模型网关
        self._injected_gateway = gateway
        try:
            self._build_model_layers()
        except ValueError as exc:
            # 缺少 API Key 不影响服务启动，上传时给出明确错误
            self._gateway_error = str(exc)
            logger.warning("模型未配置：%s", exc)

    def _build_model_layers(self) -> None:
        self.gateway = self._injected_gateway or build_gateway(
            self.settings, gates=self.gates
        )
        self.pipeline = Pipeline(self.settings, self.store, self.gateway)
        self.pipeline.gates = self.gates

    def _ensure_graph(self) -> None:
        """惰性构建工作流图。

        `langgraph` 导入约 0.4s，只有真正跑任务才需要；放到第一次 start 时再付，
        让每次重启服务省掉这部分导入时间。
        """

        if self.graph is not None:
            return
        from app.workflow.graph import NODE_ORDER, AnalysisGraph

        self.graph = AnalysisGraph(
            {name: getattr(self.pipeline, name) for name in NODE_ORDER}
        )

    # ------------------------------------------------------------ 创建/启动
    def create_task(
        self,
        uploads: UploadedDocument | list[UploadedDocument],
        context: dict[str, Any] | None = None,
    ) -> TaskSnapshot:
        files = uploads if isinstance(uploads, list) else [uploads]
        if not files:
            raise TaskError("FILE_EMPTY", "请至少上传一个文件")
        if len(files) > self.settings.max_upload_files:
            raise TaskError(
                "FILE_COUNT_EXCEEDED",
                f"单次最多上传 {self.settings.max_upload_files} 个文件，请重新选择",
            )
        for upload in files:
            self._validate_upload(upload)

        first = files[0]
        task_id = self.store.new_task_id(first.filename)
        stored = []
        for upload in files:
            path = self.store.save_upload(task_id, upload)
            stored.append(
                {
                    "name": Path(upload.filename or path.name).name,
                    "path": str(path),
                    "size": upload.size,
                }
            )

        created_at = datetime.now().astimezone()
        source_name = (
            stored[0]["name"]
            if len(stored) == 1
            else f"{stored[0]['name']} 等 {len(stored)} 个文件"
        )
        status = {
            "task_id": task_id,
            "source_name": source_name,
            "status": "queued",
            "progress": 0,
            "step": "已排队",
            "error": None,
            "created_at": created_at.isoformat(),
            "finished_at": None,
            "house_bill_count": 0,
            "manifest_entry_count": 0,
            "inconsistent_count": 0,
            "context": context or {},
            # 单文件兼容字段；多文件时 files 才是权威来源
            "uploaded_path": stored[0]["path"],
            "files": stored,
        }
        self._write_status(task_id, status)
        logger.info("创建任务 %s（%s）", task_id, status["source_name"])
        return TaskSnapshot(**status)

    def create_and_start(
        self,
        uploads: UploadedDocument | list[UploadedDocument],
        context: dict[str, Any] | None = None,
    ) -> TaskSnapshot:
        snapshot = self.create_task(uploads, context)
        self.start(snapshot.task_id)
        return snapshot

    def start(self, task_id: str) -> None:
        if self.pipeline is None:
            status = self._read_status(task_id)
            status.update(
                status="failed",
                step="处理失败",
                error={
                    "code": "MODEL_NOT_CONFIGURED",
                    "message": self._gateway_error
                    or "未配置模型密钥（DOCMIND_DZ_MODEL_API_KEY），无法执行抽取",
                },
                finished_at=datetime.now().astimezone().isoformat(),
            )
            self._write_status(task_id, status)
            return
        self._ensure_graph()
        if task_id in self._running and not self._running[task_id].done():
            return
        status = self._read_status(task_id)
        if status.get("status") in TERMINAL_STATUSES:
            return
        if status.get("interrupt_requested"):
            status["interrupt_requested"] = None
            self._write_status(task_id, status)
        task = asyncio.create_task(self._run(task_id), name=f"analysis-{task_id}")
        self._running[task_id] = task

    # ------------------------------------------------------------ 执行
    async def _run(self, task_id: str) -> None:
        status = self._read_status(task_id)
        if status.get("status") in TERMINAL_STATUSES:
            return
        waiting = self.gates.waiting("task")
        if waiting:
            await self.emit_event(
                task_id,
                "排队调度",
                f"等待执行槽位（前面 {waiting} 个任务）",
                int(status.get("progress") or 0),
            )
        queued_at = time.perf_counter()
        update: dict[str, Any] = {}
        try:
            assert self.graph is not None
            # 排队期间快照保持 queued（与 docmind 一致），拿到槽位后才切 running
            async with self.gates.acquire(
                "task", self.settings.max_concurrent_tasks
            ) as ahead:
                waited = time.perf_counter() - queued_at
                status = self._read_status(task_id)
                if status.get("status") in TERMINAL_STATUSES:
                    return
                if status.get("interrupt_requested"):
                    self._park_task(task_id, self._interrupt_kind(task_id))
                    return
                status.update(
                    status="running",
                    step="开始处理",
                    progress=1,
                    error=None,
                    interrupt_requested=None,
                )
                self._write_status(task_id, status)
                if waited >= 1 or ahead:
                    await self.emit_event(
                        task_id,
                        "排队调度",
                        f"排队 {waited:.1f} 秒后获得执行槽位",
                        1,
                    )
                state = self._build_state(task_id, status)
                update = await self.graph.ainvoke(state)  # type: ignore[arg-type]
        except TaskPausedError:
            # 节点入口命中暂停：状态已由 pause_task 落盘，这里幂等收尾
            self._park_task(task_id, "pause")
            return
        except TaskCancelledError:
            self._park_task(task_id, "cancel")
            return
        except asyncio.CancelledError:
            # pause/cancel 用 task.cancel() 打断在途 await：按落盘的中断意图收尾
            self._park_task(task_id, self._interrupt_kind(task_id))
            return
        except PipelineError as exc:
            await self._fail(task_id, exc.code, exc.message)
            return
        except Exception as exc:  # noqa: BLE001 - 兜底避免任务永远停在 running
            logger.exception("任务 %s 执行失败", task_id)
            code = "MODEL_CALL_FAILED" if "调用失败" in str(exc) else "INTERNAL_ERROR"
            await self._fail(task_id, code, f"分析任务执行失败：{exc}")
            return
        finally:
            self._running.pop(task_id, None)

        result = update.get("result")
        if not result:
            await self._fail(task_id, "INTERNAL_ERROR", "任务未产出结果文件")
            return
        summary = result.get("summary") or {}
        status.update(
            status="succeeded",
            step="处理完成",
            progress=100,
            finished_at=datetime.now().astimezone().isoformat(),
            house_bill_count=summary.get("total_house_bills", 0),
            manifest_entry_count=summary.get("total_manifest_entries", 0),
            inconsistent_count=summary.get("inconsistent", 0),
        )
        self._write_status(task_id, status)

    def _build_state(self, task_id: str, status: dict[str, Any]) -> dict[str, Any]:
        from app.workflow.state import AnalysisState

        state: AnalysisState = {
            "task_id": task_id,
            "uploaded_path": status.get("uploaded_path", ""),
            "files": status.get("files") or [],
            "source_name": status.get("source_name", ""),
            "context": status.get("context") or {},
            "created_at": status.get("created_at"),
            "reporter": TaskReporter(self, task_id),
            "completed_nodes": self._completed_nodes(task_id),
        }
        # 断点续跑：跳过已完成节点时，state 里没有上游产物，需从磁盘回填
        doc = load_document(self.store, task_id)
        if doc.get("pages"):
            state["pages"] = [page.model_dump() for page in doc["pages"]]
            state["parser"] = doc.get("parser", "")
            state["page_boundaries"] = doc.get("page_boundaries", [])
            state["warnings"] = list(doc.get("warnings") or [])
            if doc.get("files"):
                state["files"] = doc["files"]
            if doc.get("segments"):
                state["segments"] = doc["segments"]
                state["master_bills"] = doc.get("master_bills", [])

        if self.store.documents_path(task_id).exists():
            house_bills, manifest, doc_warnings = load_documents(self.store, task_id)
            state["house_bills"] = house_bills
            state["manifest"] = manifest
            state["warnings"] = list(
                dict.fromkeys([*state.get("warnings", []), *doc_warnings])
            )

        comparisons_doc = (
            self.store.read_json_or_none(self.store.comparisons_path(task_id)) or {}
        )
        if comparisons_doc:
            state["comparisons"] = comparisons_doc.get("comparisons", [])
            state["totals_checks"] = comparisons_doc.get("totals_checks", [])
            state["summary"] = comparisons_doc.get("summary", {})
            state["warnings"] = list(
                dict.fromkeys(
                    [
                        *state.get("warnings", []),
                        *(comparisons_doc.get("warnings") or []),
                    ]
                )
            )
        return dict(state)

    def _completed_nodes(self, task_id: str) -> list[str]:
        """按磁盘产物推断已完成节点，仅返回连续前缀，避免跳节点。"""

        completed: list[str] = []
        doc = self.store.read_json_or_none(self.store.document_path(task_id)) or {}
        if doc.get("pages"):
            completed.append("render_document")
            if doc.get("transcribed"):
                completed.append("transcribe_pages")
            else:
                return completed
        else:
            return completed
        segments = doc.get("segments") or []
        if segments:
            completed.append("classify_pages")
        else:
            return completed
        house_indexes = [
            index
            for segment in segments
            if segment.get("kind") == "house_bill"
            for index in segment.get("page_indexes") or []
        ]
        if not house_indexes:
            # 只有主单/舱单：路由会跳过局部精读、抽取与核对，视为全部完成
            completed.extend(
                ["transcribe_house_zoom", "extract_documents", "verify_consistency"]
            )
            return completed
        if self.store.zoom_status_path(task_id).exists():
            completed.append("transcribe_house_zoom")
        else:
            return completed
        if self.store.documents_path(task_id).exists():
            completed.append("extract_documents")
        else:
            return completed
        if self.store.comparisons_path(task_id).exists():
            completed.append("verify_consistency")
        return completed

    async def _fail(self, task_id: str, code: str, message: str) -> None:
        status = self._read_status(task_id)
        status.update(
            status="failed",
            step="处理失败",
            error={"code": code, "message": message},
            finished_at=datetime.now().astimezone().isoformat(),
        )
        self._write_status(task_id, status)
        await self.emit_event(task_id, "处理失败", message, status.get("progress", 0), "error")

    # ------------------------------------------------------------ 事件/快照
    async def emit_event(
        self,
        task_id: str,
        step: str,
        message: str,
        progress: int,
        level: str = "info",
        *,
        phase: str = "",
        duration_ms: int | None = None,
    ) -> None:
        async with self._lock:
            seq = self._next_seq(task_id)
            event = WorkflowEvent(
                seq=seq,
                at=datetime.now().astimezone(),
                level=level,  # type: ignore[arg-type]
                step=step,
                message=message,
                progress=max(0, min(progress, 100)),
                phase=phase,  # type: ignore[arg-type]
                duration_ms=duration_ms,
            )
            self.store.append_line(
                self.store.process_log_path(task_id),
                event.model_dump_json(),
            )
            status = self._read_status(task_id)
            # 冻结态（暂停/终态）只追加时间线，不再改写状态，避免收尾事件覆盖
            if status.get("status") not in FROZEN_STATUSES:
                status["step"] = step
                status["progress"] = max(int(status.get("progress") or 0), event.progress)
                self._write_status(task_id, status)

    def _next_seq(self, task_id: str) -> int:
        log_path = self.store.process_log_path(task_id)
        if not log_path.exists():
            return 1
        try:
            with log_path.open("rb") as file:
                return sum(1 for _ in file) + 1
        except OSError:
            return 1

    def get_snapshot(self, task_id: str) -> TaskSnapshot:
        snapshot = TaskSnapshot(**self._read_status(task_id))
        snapshot.result_available = self.store.result_path(task_id).exists()
        return snapshot

    def list_tasks(self) -> list[TaskSnapshot]:
        snapshots: list[TaskSnapshot] = []
        for task_dir in (self.store.root / "tasks").iterdir():
            status_path = task_dir / "task_status.json"
            if not status_path.exists():
                continue
            data = self.store.read_json_or_none(status_path)
            if isinstance(data, dict):
                try:
                    snapshot = TaskSnapshot(**data)
                except ValueError:
                    continue
                snapshot.result_available = self.store.result_path(
                    snapshot.task_id
                ).exists()
                snapshots.append(snapshot)
        snapshots.sort(key=lambda item: item.created_at, reverse=True)
        return snapshots

    def read_events(self, task_id: str, from_seq: int = 0) -> list[WorkflowEvent]:
        log_path = self.store.process_log_path(task_id)
        if not log_path.exists():
            return []
        events: list[WorkflowEvent] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = WorkflowEvent.model_validate_json(line)
            except ValueError:
                continue
            if event.seq > from_seq:
                events.append(event)
        return events

    def get_result(self, task_id: str) -> dict[str, Any]:
        self._read_status(task_id)
        result = self.store.read_json_or_none(self.store.result_path(task_id))
        if result is None:
            status = self._read_status(task_id)
            if status.get("status") == "failed":
                error = status.get("error") or {}
                raise TaskError(
                    error.get("code", "TASK_FAILED"),
                    error.get("message", "任务失败，无结果"),
                    http_status=409,
                )
            raise TaskError("RESULT_NOT_READY", "任务尚未完成，请稍后再试", http_status=409)
        return result

    def submit(self, task_id: str, payload: SubmitRequest) -> SubmitResponse:
        """保存人工核对后的数据，并把任务标记为已提交。

        本服务没有外部订舱系统，提交结果以 submission.json 落盘并生成提交编号；
        幂等键相同的重复提交直接返回首次结果。
        """

        status = self._read_status(task_id)
        if status.get("status") != "succeeded":
            raise TaskError("TASK_NOT_READY", "任务尚未成功完成，无法提交", http_status=409)
        if not payload.documents:
            raise TaskError("SUBMIT_EMPTY", "提交内容为空", http_status=400)

        existing = self.store.read_json_or_none(self.store.submission_path(task_id))
        if (
            isinstance(existing, dict)
            and payload.request_id
            and existing.get("request_id") == payload.request_id
        ):
            return SubmitResponse(
                ok=True,
                submission_code=existing.get("submission_code", ""),
                submitted_at=existing.get("submitted_at", ""),
                duplicated=True,
            )

        inconsistent = int(status.get("inconsistent_count") or 0)
        if inconsistent > 0 and not payload.acknowledged:
            raise TaskError(
                "DIFF_NOT_ACKNOWLEDGED",
                f"存在 {inconsistent} 份分单与舱单不一致，请先核对差异并勾选确认",
                http_status=409,
            )

        submitted_at = datetime.now().astimezone()
        code = f"DZ{submitted_at.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"
        record = {
            "request_id": payload.request_id,
            "submission_code": code,
            "submitted_at": submitted_at.isoformat(),
            "acknowledged": payload.acknowledged,
            "documents": [item.model_dump() for item in payload.documents],
        }
        self.store.write_json_atomic(self.store.submission_path(task_id), record)
        status.update(
            submitted=True,
            submitted_at=submitted_at.isoformat(),
            submission_code=code,
        )
        self._write_status(task_id, status)
        logger.info("任务 %s 已提交：%s", task_id, code)
        return SubmitResponse(
            ok=True,
            submission_code=code,
            submitted_at=submitted_at.isoformat(),
            duplicated=False,
        )

    def get_submission(self, task_id: str) -> dict[str, Any] | None:
        self._read_status(task_id)
        record = self.store.read_json_or_none(self.store.submission_path(task_id))
        return record if isinstance(record, dict) else None

    def search_ports(self, keyword: str) -> dict[str, Any]:
        """港口三字码搜索：只查本地 hbinfo.json，不调模型。"""

        from app.service.port_reference import PortReferenceIndex

        index = PortReferenceIndex.load(self.settings.data_root)
        if index is None:
            return {"enabled": False, "items": []}
        return {"enabled": True, "items": index.search(keyword)}

    def page_image(self, task_id: str, index: int) -> Path:
        self._read_status(task_id)
        # 多文件任务的图片在 src01/、src02/ 子目录，以 document.json 的 image_rel 为准
        for page in load_pages_meta(self.store, task_id):
            if page.get("index") != index:
                continue
            relative = page.get("image_rel")
            if relative:
                try:
                    path = self.store.resolve_relative(relative)
                except ValueError:
                    break
                if path.exists():
                    return path
            break
        legacy = self.store.page_image_path(task_id, index)
        if legacy.exists():
            return legacy
        raise TaskError("PAGE_NOT_FOUND", f"第 {index} 页图片不存在", http_status=404)

    # ------------------------------------------------------------ 中断控制
    def pause_task(self, task_id: str) -> TaskSnapshot:
        """暂停在途任务：落 paused 并中断当前 await，已产出的节点保留。"""

        status = self._read_status(task_id)
        if status.get("status") in FROZEN_STATUSES:
            return TaskSnapshot(**status)
        self._park_task(task_id, "pause")
        self._cancel_handle(task_id)
        return self.get_snapshot(task_id)

    def resume_task(self, task_id: str) -> TaskSnapshot:
        """从暂停处继续：只执行没有落盘产物的节点。"""

        status = self._read_status(task_id)
        if status.get("status") != "paused":
            return TaskSnapshot(**status)
        status.update(
            status="queued",
            step="继续执行",
            error=None,
            interrupt_requested=None,
            finished_at=None,
        )
        self._write_status(task_id, status)
        self.start(task_id)
        return self.get_snapshot(task_id)

    def cancel_task(self, task_id: str) -> TaskSnapshot:
        """取消任务：不可恢复，停在当前进度且不写 error。"""

        status = self._read_status(task_id)
        if status.get("status") in TERMINAL_STATUSES:
            return TaskSnapshot(**status)
        self._park_task(task_id, "cancel")
        self._cancel_handle(task_id)
        return self.get_snapshot(task_id)

    async def checkpoint(self, task_id: str) -> None:
        """节点入口的中断检查点：命中暂停/取消时抛出对应异常。"""

        try:
            status = self._read_status(task_id)
        except TaskNotFoundError:
            return
        requested = status.get("interrupt_requested")
        if requested == "pause" or status.get("status") == "paused":
            raise TaskPausedError(f"任务已暂停：{task_id}")
        if requested == "cancel" or status.get("status") == "cancelled":
            raise TaskCancelledError(f"任务已取消：{task_id}")

    def _cancel_handle(self, task_id: str) -> None:
        handle = self._running.get(task_id)
        if handle is not None and not handle.done():
            handle.cancel()

    def _park_task(self, task_id: str, kind: str) -> None:
        """按中断类型落盘（幂等），并追加一条时间线事件。"""

        try:
            status = self._read_status(task_id)
        except TaskNotFoundError:
            return
        if kind == "pause":
            if status.get("status") in FROZEN_STATUSES:
                return
            status.update(status="paused", step="已暂停", interrupt_requested="pause")
            message = "任务已暂停，可从断点继续"
        else:
            if status.get("status") in TERMINAL_STATUSES:
                return
            status.update(
                status="cancelled",
                step="已取消",
                interrupt_requested="cancel",
                finished_at=datetime.now().astimezone().isoformat(),
            )
            message = "任务已取消"
        self._write_status(task_id, status)
        event = WorkflowEvent(
            seq=self._next_seq(task_id),
            at=datetime.now().astimezone(),
            level="warning",
            step=status["step"],
            message=message,
            progress=int(status.get("progress") or 0),
        )
        self.store.append_line(
            self.store.process_log_path(task_id), event.model_dump_json()
        )

    def _interrupt_kind(self, task_id: str) -> str:
        try:
            status = self._read_status(task_id)
        except TaskNotFoundError:
            return "cancel"
        return "pause" if status.get("interrupt_requested") == "pause" else "cancel"

    # ------------------------------------------------------------ 校验/文件
    def _validate_upload(self, upload: UploadedDocument) -> None:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in self.settings.allowed_extensions:
            raise TaskError(
                "FILE_TYPE_NOT_SUPPORTED",
                f"暂不支持的文件类型：{suffix or '无后缀'}，"
                "支持 PDF / Word / Excel",
            )
        if upload.size == 0:
            raise TaskError("FILE_EMPTY", "上传文件为空")
        max_bytes = self.settings.max_file_size_mb * 1024 * 1024
        if upload.size > max_bytes:
            raise TaskError(
                "FILE_TOO_LARGE",
                f"文件超过 {self.settings.max_file_size_mb} MB 限制",
            )
        head = upload.content[:16]
        if suffix == ".pdf" and not upload.content.startswith(b"%PDF"):
            raise TaskError("FILE_CONTENT_INVALID", "文件内容不是有效的 PDF")
        if suffix in {".docx", ".xlsx"} and not head.startswith(b"PK\x03\x04"):
            raise TaskError("FILE_CONTENT_INVALID", "文件内容不是有效的 Office 文档")
        if suffix in {".doc", ".xls"} and not head.startswith(
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        ):
            raise TaskError("FILE_CONTENT_INVALID", "文件内容不是有效的旧版 Office 文档")
        image_signatures = {
            ".png": (b"\x89PNG",),
            ".jpg": (b"\xff\xd8\xff",),
            ".jpeg": (b"\xff\xd8\xff",),
            ".bmp": (b"BM",),
            ".tif": (b"II*\x00", b"MM\x00*"),
            ".tiff": (b"II*\x00", b"MM\x00*"),
            ".webp": (b"RIFF",),
        }
        if suffix in image_signatures and not any(
            head.startswith(signature) for signature in image_signatures[suffix]
        ):
            raise TaskError("FILE_CONTENT_INVALID", "文件内容不是有效的图片")

    def _read_status(self, task_id: str) -> dict[str, Any]:
        path = self.store.task_status_path(task_id)
        if not path.exists():
            raise TaskNotFoundError(task_id)
        data = self.store.read_json_or_none(path)
        if not isinstance(data, dict):
            raise TaskNotFoundError(task_id)
        return data

    def _write_status(self, task_id: str, status: dict[str, Any]) -> None:
        self.store.write_json_atomic(self.store.task_status_path(task_id), status)

    # ------------------------------------------------------------ 运维
    def cleanup_expired(self) -> int:
        return self.store.cleanup_expired(self.settings.data_retention_hours)
