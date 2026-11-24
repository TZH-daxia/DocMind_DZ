"""端到端管线（注入假网关，不调用真实模型）。"""

import asyncio

import pytest

from app.schemas.file import UploadedDocument
from app.service.task_service import TaskError, TaskService
from tests.conftest import (
    MANIFEST_PAGE,
    MASTER_PAGE,
    house_page,
    make_service,
    write_pdf,
)


async def _run_task(service: TaskService, task_id: str) -> None:
    service.start(task_id)
    for _ in range(200):
        snapshot = service.get_snapshot(task_id)
        if snapshot.status in {"succeeded", "failed"}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("任务未在预期时间内结束")


async def test_pipeline_uses_manifest_rows_as_house_bills(settings, tmp_path):
    """有舱单时以舱单为准：舱单行转换成分单，不再抽分单页、也不再做核对。"""

    pdf = write_pdf(
        tmp_path / "bundle.pdf",
        [
            MASTER_PAGE,
            house_page("HAWB0001"),
            house_page("HAWB0002"),
            MANIFEST_PAGE,
        ],
    )
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="bundle.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)

    snapshot = service.get_snapshot(snapshot.task_id)
    assert snapshot.status == "succeeded", snapshot.error
    result = service.get_result(snapshot.task_id)

    assert result["page_count"] == 4
    assert len(result["master_bills"]) == 1
    master = result["master_bills"][0]
    assert master["skipped"] is True
    assert master["page_indexes"] == [1]
    assert master["master_no"] == "999-12345678"

    # 结果里只有分单（舱单行转换而来）；舱单明细不再随结果返回，只留 summary 计数
    house_bills = result["house_bills"]
    assert len(house_bills) > 0
    assert all(bill["source"] == "manifest" for bill in house_bills)
    assert all(bill["page_indexes"] == [4] for bill in house_bills)
    first = house_bills[0]
    assert first["fields"]["hawb"]
    assert first["fields"]["mdg"]

    # 不再做一致性核对
    assert result["comparisons"] == []
    assert result["totals_checks"] == []
    summary = result["summary"]
    assert summary["total_house_bills"] == len(house_bills)
    assert summary["total_manifest_entries"] > 0
    assert summary["inconsistent"] == 0


async def test_pipeline_succeeds_with_master_only_file(settings, tmp_path):
    pdf = write_pdf(tmp_path / "master_only.pdf", [MASTER_PAGE])
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="master_only.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)

    snapshot = service.get_snapshot(snapshot.task_id)
    assert snapshot.status == "succeeded"
    result = service.get_result(snapshot.task_id)
    assert result["house_bills"] == []
    assert result["summary"]["total_manifest_entries"] == 0
    assert len(result["master_bills"]) == 1
    assert any("主单" in warning for warning in result["warnings"])


async def test_manifest_only_file_fails_without_master_number(settings, tmp_path):
    """只有舱单、且舱单里读不到主单号：任务失败（NO_MAWB）。

    需求：没有主单号就判定失败 —— 主单号是 ERP 入库的必需条件，
    与其「成功但一条都用不了」，不如直接报错让用户重新上传。
    """

    pdf = write_pdf(tmp_path / "manifest_only.pdf", [MANIFEST_PAGE])
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="manifest_only.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)

    snapshot = service.get_snapshot(snapshot.task_id)
    assert snapshot.status == "failed"
    assert snapshot.error is not None
    assert snapshot.error.code == "NO_MAWB"


async def test_master_and_manifest_rows_become_house_bills(settings, tmp_path):
    """主单 + 舱单：舱单行转换成分单，不做核对。"""

    pdf = write_pdf(tmp_path / "master_manifest.pdf", [MASTER_PAGE, MANIFEST_PAGE])
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="master_manifest.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)

    assert service.get_snapshot(snapshot.task_id).status == "succeeded"
    result = service.get_result(snapshot.task_id)
    assert len(result["house_bills"]) > 0
    assert all(bill["source"] == "manifest" for bill in result["house_bills"])
    # 结果不含舱单明细，行数只在 summary 里
    assert "manifest" not in result
    assert result["summary"]["total_manifest_entries"] > 0
    # 不做核对
    assert result["comparisons"] == []
    assert result["totals_checks"] == []
    assert result["summary"]["inconsistent"] == 0

    events = service.read_events(snapshot.task_id)
    skipped = {event.step for event in events if event.phase == "skipped"}
    started = {event.step for event in events if event.phase == "started"}
    assert "核对分单与舱单" in skipped
    assert "抽取分单或舱单字段" in started


async def test_house_only_file_skips_compare(settings, tmp_path):
    """只有分单：正常抽取，但不做一致性核对，也不该出现假差异。"""

    pdf = write_pdf(tmp_path / "house_only.pdf", [house_page("HAWB0001")])
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="house_only.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)

    assert service.get_snapshot(snapshot.task_id).status == "succeeded"
    result = service.get_result(snapshot.task_id)
    assert len(result["house_bills"]) == 1
    assert result["summary"]["total_manifest_entries"] == 0
    assert result["comparisons"] == []
    assert result["summary"]["inconsistent"] == 0
    assert result["summary"]["total_house_bills"] == 1


async def test_master_only_file_skips_extract_and_verify_nodes(settings, tmp_path):
    """只有主单时，图应直接跳到结果装配，不执行抽取与核对节点。"""

    pdf = write_pdf(tmp_path / "master_single.pdf", [MASTER_PAGE])
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="master_single.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)

    assert service.get_snapshot(snapshot.task_id).status == "succeeded"
    events = service.read_events(snapshot.task_id)
    skipped = {event.step for event in events if event.phase == "skipped"}
    started = {event.step for event in events if event.phase == "started"}
    assert skipped == {"抽取分单或舱单字段", "核对分单与舱单"}
    assert "抽取分单或舱单字段" not in started
    assert "核对分单与舱单" not in started


async def test_progress_only_moves_on_node_boundaries(settings):
    """进度按节点完成推进：节点内部的细粒度事件只进时间线，不改进度。"""

    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="a.pdf", content=b"%PDF-1.4 progress")
    )
    task_id = snapshot.task_id

    # 节点内部事件（phase 为空）：进度不动
    await service.emit_event(task_id, "解析文档", "解析中…", 80)
    assert service.get_snapshot(task_id).progress == 0

    # 节点完成：进度推到下一个节点的入口值
    await service.emit_event(task_id, "解析文档", "解析文档完成", 25, phase="succeeded")
    assert service.get_snapshot(task_id).progress == 25

    # 后续节点内部事件仍然不动
    await service.emit_event(task_id, "图像转写", "逐页转写 3/10", 90)
    assert service.get_snapshot(task_id).progress == 25


def test_correct_emails_uses_text_layer(settings, tmp_path):
    """VLM 漏字符的邮箱按 PDF 文本层纠正；页面没有的邮箱保持原值。"""

    pdf = write_pdf(
        tmp_path / "manifest_email.pdf",
        ["SHIPPER EMAIL yejianjing@lanshenglight.com"],
    )
    service = make_service(settings)
    fields = {
        "email_fhr_hawb": "yejianjing@lanshenglght.com",  # 少一个 i
        "email_shr_hawb": "someone@other.com",  # 页面里没有
    }

    warnings = service.pipeline._correct_emails(fields, pdf, {1: 1})

    assert fields["email_fhr_hawb"] == "yejianjing@lanshenglight.com"
    assert fields["email_shr_hawb"] == "someone@other.com"
    assert warnings
    assert "发货人邮箱" in warnings[0]


def test_reject_unsupported_extension(settings):
    service = make_service(settings)
    with pytest.raises(TaskError) as excinfo:
        service.create_task(UploadedDocument(filename="a.txt", content=b"hello"))
    assert excinfo.value.code == "FILE_TYPE_NOT_SUPPORTED"


def test_reject_fake_pdf(settings):
    service = make_service(settings)
    with pytest.raises(TaskError) as excinfo:
        service.create_task(UploadedDocument(filename="a.pdf", content=b"not a pdf"))
    assert excinfo.value.code == "FILE_CONTENT_INVALID"


def test_reject_too_large(settings):
    service = make_service(settings.model_copy(update={"max_file_size_mb": 1}))
    with pytest.raises(TaskError) as excinfo:
        service.create_task(
            UploadedDocument(filename="big.pdf", content=b"%PDF" + b"0" * (2 * 1024 * 1024))
        )
    assert excinfo.value.code == "FILE_TOO_LARGE"


async def test_single_mode_merges_multiple_files_into_one_task(settings, tmp_path):
    """单票：同一主单的多个文件合成一个任务，页面按文件顺序拼接。"""

    first = write_pdf(tmp_path / "part1.pdf", [MASTER_PAGE])
    second = write_pdf(
        tmp_path / "part2.pdf", [house_page("HAWB0001"), MANIFEST_PAGE]
    )
    service = make_service(settings)
    snapshot = service.create_task(
        [
            UploadedDocument(filename="part1.pdf", content=first.read_bytes()),
            UploadedDocument(filename="part2.pdf", content=second.read_bytes()),
        ],
        mode="single",
    )
    await _run_task(service, snapshot.task_id)

    snapshot = service.get_snapshot(snapshot.task_id)
    assert snapshot.status == "succeeded", snapshot.error
    assert snapshot.mode == "single"
    assert [item["name"] for item in snapshot.files] == ["part1.pdf", "part2.pdf"]

    result = service.get_result(snapshot.task_id)
    assert result["page_count"] == 3
    assert len(result["master_bills"]) == 1
    # 有舱单时以舱单为准：分单来自舱单行（舱单明细不再随结果返回）
    assert len(result["house_bills"]) > 0
    assert result["summary"]["total_manifest_entries"] > 0
    assert all(bill["source"] == "manifest" for bill in result["house_bills"])


async def test_single_mode_fails_on_multiple_master_bills(settings, tmp_path):
    """单票：多个文件必须是同一个主单，识别出两个主单直接失败。"""

    first = write_pdf(tmp_path / "a.pdf", [MASTER_PAGE])
    second = write_pdf(
        tmp_path / "b.pdf",
        [MASTER_PAGE.replace("999-12345678", "888-12345678")],
    )
    service = make_service(settings)
    snapshot = service.create_task(
        [
            UploadedDocument(filename="a.pdf", content=first.read_bytes()),
            UploadedDocument(filename="b.pdf", content=second.read_bytes()),
        ],
        mode="single",
    )
    await _run_task(service, snapshot.task_id)

    snapshot = service.get_snapshot(snapshot.task_id)
    assert snapshot.status == "failed"
    assert snapshot.error is not None
    assert snapshot.error.code == "SINGLE_TICKET_MULTIPLE_MASTER"


async def test_single_mode_detects_master_no_in_house_page_head(settings, tmp_path):
    """第二个主单号只印在分单页页首（不形成主单段）时，也要在分类阶段拦截。"""

    # 票 A：分单页左上角印着另一个主单号，整页会被判成 house_bill
    first = write_pdf(
        tmp_path / "a.pdf",
        [house_page("HAWB0001").replace("999-12345678", "369-10799445")],
    )
    second = write_pdf(tmp_path / "b.pdf", [MASTER_PAGE])
    service = make_service(settings)
    snapshot = service.create_task(
        [
            UploadedDocument(filename="a.pdf", content=first.read_bytes()),
            UploadedDocument(filename="b.pdf", content=second.read_bytes()),
        ],
        mode="single",
    )
    await _run_task(service, snapshot.task_id)

    snapshot = service.get_snapshot(snapshot.task_id)
    assert snapshot.status == "failed"
    assert snapshot.error is not None
    assert snapshot.error.code == "SINGLE_TICKET_MULTIPLE_MASTER"
    assert "369-10799445" in snapshot.error.message
    assert "999-12345678" in snapshot.error.message


def test_batch_mode_accepts_single_file_only(settings, tmp_path):
    """批量：一个文件一个任务，因此接口层拒绝一次传多个文件。"""

    pdf = write_pdf(tmp_path / "a.pdf", [MASTER_PAGE])
    service = make_service(settings)
    with pytest.raises(TaskError) as excinfo:
        service.create_task(
            [
                UploadedDocument(filename="a.pdf", content=pdf.read_bytes()),
                UploadedDocument(filename="b.pdf", content=pdf.read_bytes()),
            ],
            mode="batch",
        )
    assert excinfo.value.code == "BATCH_MODE_SINGLE_FILE"

def test_canonical_master_no_ignores_forwarder_numbers():
    """单票多主单判定只认 3 位航司代码 + 8 位序号，分单号不参与。"""

    from app.service.pipeline import _canonical_master_no

    assert _canonical_master_no("41652342905") == "416-52342905"
    assert _canonical_master_no("416-52342905") == "416-52342905"
    assert _canonical_master_no("SZA26100005") is None
    assert _canonical_master_no("") is None
    assert _canonical_master_no(None) is None


def test_degenerate_transcription_is_detected():
    """视觉转写读成一堆空单元格时要能被识别出来（回退文本层）。"""

    from app.service.pipeline import _is_degenerate_transcription

    broken = "416 | PVG | 52342905 " + "| " * 400
    assert _is_degenerate_transcription(broken) is True

    normal = (
        "CARGO MANIFEST\n"
        "HAWB NO. | ORIGIN | DEST | FLIGHT | PCS | G.W.\n"
        "WPL26100101 | PVG | DFW | K4917 | 45 | 6687\n"
        "REFRIGERANT PUMP\n"
    )
    assert _is_degenerate_transcription(normal) is False
