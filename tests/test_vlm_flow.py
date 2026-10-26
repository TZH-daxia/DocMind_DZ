"""全量视觉转写与分单四象限局部精读（不依赖真实模型）。"""

from __future__ import annotations

from pathlib import Path

from app.schemas.documents import PageRecord, Segment
from app.service.extraction import format_pages
from app.service.pipeline import Pipeline
from app.storage.file_store import FileStore
from tests.conftest import write_pdf

TASK_ID = "task_test_vlm"


class _FakeVisionGateway:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[int] = []

    async def complete_vision(
        self,
        label,
        system_prompt,
        human_prompt,
        image_paths=None,
        *,
        image_bytes=None,
    ) -> str:
        self.calls.append(len(image_bytes or image_paths or []))
        return self.text


def _setup(settings, tmp_path: Path, gateway) -> tuple[FileStore, Pipeline]:
    store = FileStore(settings)
    task_dir = store.task_dir(TASK_ID)
    write_pdf(task_dir / "awb.pdf", ["HOUSE AIR WAYBILL\nHAWB NO.: HAWB0001"])
    return store, Pipeline(settings, store, gateway)


def _pages() -> list[PageRecord]:
    return [
        PageRecord(
            index=1,
            text="文本层原文",
            text_source="text_layer",
            image_rel=f"tasks/{TASK_ID}/src01/page_001.png",
            source_file="awb.pdf",
            page_in_file=1,
        )
    ]


async def test_all_pages_go_through_vision(settings, tmp_path):
    """有页面图就不再用文本层：整页转写覆盖原文并单独落盘 md。"""

    gateway = _FakeVisionGateway("VLM 转写文本")
    store, pipeline = _setup(settings, tmp_path, gateway)
    pages = _pages()

    result = await pipeline.transcribe_pages(
        {
            "task_id": TASK_ID,
            "pages": [page.model_dump() for page in pages],
            "warnings": [],
            "parser": "pdf",
        }
    )

    page = PageRecord(**result["pages"][0])
    assert page.text == "VLM 转写文本"
    assert page.text_source == "vlm"
    assert gateway.calls == [1]
    assert (
        store.transcription_path(TASK_ID, 1).read_text(encoding="utf-8")
        == "VLM 转写文本"
    )


async def test_house_zoom_writes_text_images_and_is_idempotent(settings, tmp_path):
    """分单页四象限精读：4 张切图落盘、文本写回页面、重复执行不重复调模型。"""

    gateway = _FakeVisionGateway("### 左上（part 1）\nLAX")
    store, pipeline = _setup(settings, tmp_path, gateway)
    pages = _pages()
    segments = [Segment(segment_id="seg_001", kind="house_bill", page_indexes=[1])]
    state = {
        "task_id": TASK_ID,
        "pages": [page.model_dump() for page in pages],
        "segments": [segment.model_dump() for segment in segments],
        "parser": "pdf",
        "warnings": [],
        "files": [
            {
                "name": "awb.pdf",
                "path": str(store.task_dir(TASK_ID) / "awb.pdf"),
                "size": 1,
            }
        ],
        "page_boundaries": [1],
    }

    result = await pipeline.transcribe_house_zoom(state)

    page = PageRecord(**result["pages"][0])
    assert page.zoom_text == "### 左上（part 1）\nLAX"
    assert page.zoom_rel
    assert Path(store.resolve_relative(page.zoom_rel)).exists()
    # 四象限切图只在内存里传给模型，不落盘到任务目录
    assert not list(store.task_dir(TASK_ID).glob("page_*_part_*.png"))
    assert gateway.calls[-1] == 4
    assert store.zoom_status_path(TASK_ID).exists()

    assert await pipeline.transcribe_house_zoom(state) == {}
    assert gateway.calls[-1] == 4


def test_format_pages_prefers_zoom_text() -> None:
    page = PageRecord(index=1, text="整页文本", zoom_text="局部文本")

    text = format_pages([page], [1])

    assert "局部放大精读" in text and "整页转写" in text
    assert text.index("局部文本") < text.index("整页文本")
