"""任务文档模型：把「页面/分段/主单/分单/舱单/候选」收敛到三个文件。

- `document.json`：页面元数据（全文在 `transcription/page_00N.md`）+ 分段 + 主单登记；
- `candidates.json`：字段候选唯一来源；
- `documents.json`：最终抽取结果（分单字段/定位 + 舱单），不含候选。

这样同一事实只存一份：转写正文只在 md，候选只在 candidates.json，
分单/舱单字段只在 documents.json。
"""

from __future__ import annotations

from typing import Any

from app.schemas.documents import HouseBill, ManifestDocument, PageRecord
from app.storage.file_store import FileStore


def _page_payload(
    store: FileStore, task_id: str, page: PageRecord
) -> dict[str, Any]:
    """页面元数据：去掉正文，只留 md 路径。"""

    payload = page.model_dump(exclude={"text", "zoom_text"})
    payload["text_rel"] = store.relative_path(
        store.transcription_path(task_id, page.index)
    )
    payload["zoom_rel"] = (
        store.relative_path(store.transcription_path(task_id, page.index, "_zoom"))
        if page.zoom_text
        else None
    )
    return payload


def save_document(
    store: FileStore,
    task_id: str,
    pages: list[PageRecord],
    parser: str | None = None,
    warnings: list[str] | None = None,
    *,
    transcribed: bool,
    page_boundaries: list[int] | None = None,
    files: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    master_bills: list[dict[str, Any]] | None = None,
) -> None:
    """写转写正文 + document.json；未传的字段保留已有值。"""

    existing = store.read_json_or_none(store.document_path(task_id)) or {}
    if parser is None:
        parser = existing.get("parser", "")
    if warnings is None:
        warnings = existing.get("warnings") or []
    if page_boundaries is None:
        page_boundaries = existing.get("page_boundaries") or []
    if files is None:
        files = existing.get("files") or []
    if segments is None:
        segments = existing.get("segments") or []
    if master_bills is None:
        master_bills = existing.get("master_bills") or []

    payload_pages: list[dict[str, Any]] = []
    for page in pages:
        store.write_text_atomic(
            store.transcription_path(task_id, page.index), page.text or ""
        )
        if page.zoom_text:
            store.write_text_atomic(
                store.transcription_path(task_id, page.index, "_zoom"),
                page.zoom_text,
            )
        payload_pages.append(_page_payload(store, task_id, page))

    store.write_json_atomic(
        store.document_path(task_id),
        {
            "parser": parser,
            "warnings": warnings,
            "transcribed": transcribed,
            "page_boundaries": page_boundaries,
            "files": files,
            "pages": payload_pages,
            "segments": segments,
            "master_bills": master_bills,
        },
    )


def load_document(store: FileStore, task_id: str) -> dict[str, Any]:
    """读 document.json，并把 md 正文回填成 PageRecord.text / zoom_text。"""

    data = store.read_json_or_none(store.document_path(task_id)) or {}
    pages: list[PageRecord] = []
    for item in data.get("pages") or []:
        payload = dict(item)
        text_rel = payload.pop("text_rel", None)
        zoom_rel = payload.pop("zoom_rel", None)
        page = PageRecord(**payload)
        if text_rel:
            try:
                page.text = store.read_text(
                    store.resolve_relative(text_rel), errors="replace"
                )
            except OSError:
                page.text = ""
        if zoom_rel:
            try:
                page.zoom_text = store.read_text(
                    store.resolve_relative(zoom_rel), errors="replace"
                )
            except OSError:
                page.zoom_text = None
        pages.append(page)
    return {**data, "pages": pages}


def load_pages_meta(store: FileStore, task_id: str) -> list[dict[str, Any]]:
    """只读页面元数据（不加载正文），供图片查询等轻量场景。"""

    data = store.read_json_or_none(store.document_path(task_id)) or {}
    return data.get("pages") or []


def save_documents(
    store: FileStore,
    task_id: str,
    house_bills: list[HouseBill],
    manifest: ManifestDocument | None,
    warnings: list[str],
) -> None:
    """写最终抽取结果与候选：候选只进 candidates.json，documents.json 不含。"""

    store.write_json_atomic(
        store.documents_path(task_id),
        {
            "house_bills": [
                bill.model_dump(exclude={"candidates"}) for bill in house_bills
            ],
            "manifest": manifest.model_dump() if manifest is not None else None,
            "warnings": warnings,
        },
    )
    store.write_json_atomic(
        store.candidates_path(task_id),
        {
            "house_bills": [
                {
                    "bill_id": bill.bill_id,
                    "segment_id": bill.segment_id,
                    "page_indexes": bill.page_indexes,
                    "candidates": [
                        candidate.model_dump() for candidate in bill.candidates
                    ],
                }
                for bill in house_bills
            ]
        },
    )


def load_documents(
    store: FileStore, task_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """读最终结果，并把 candidates.json 合并回分单（内存/API 结构不变）。"""

    data = store.read_json_or_none(store.documents_path(task_id)) or {}
    house_bills = list(data.get("house_bills") or [])
    candidates_doc = store.read_json_or_none(store.candidates_path(task_id)) or {}
    by_bill = {
        item.get("bill_id"): item.get("candidates") or []
        for item in candidates_doc.get("house_bills") or []
    }
    for bill in house_bills:
        bill["candidates"] = by_bill.get(bill.get("bill_id"), [])
    return house_bills, data.get("manifest"), list(data.get("warnings") or [])
