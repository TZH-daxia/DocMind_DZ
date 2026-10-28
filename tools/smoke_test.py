"""端到端冒烟：生成样例 PDF → 上传 → 等待 → 打印结果摘要。

用法（服务已启动）：
    uv run python tools/smoke_test.py --base http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import MANIFEST_PAGE, MASTER_PAGE, house_page, write_pdf  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--prefix", default="/docmind_dz")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="上传真实文件（可重复），缺省时生成内置样例 PDF",
    )
    args = parser.parse_args()

    def build_uploads(work_dir: Path) -> list[tuple[str, str, bytes, str]]:
        if args.file:
            uploads = []
            for raw in args.file:
                path = Path(raw)
                mime, _ = mimetypes.guess_type(path.name)
                uploads.append(
                    (path.name, path.name, path.read_bytes(), mime or "application/octet-stream")
                )
            return uploads
        pdf = write_pdf(
            work_dir / "smoke.pdf",
            [
                MASTER_PAGE,
                house_page("HAWB0001"),
                house_page("HAWB0002"),
                MANIFEST_PAGE,
            ],
        )
        return [("smoke.pdf", "smoke.pdf", pdf.read_bytes(), "application/pdf")]

    with tempfile.TemporaryDirectory(prefix="docmind-smoke-") as temp_dir:
        uploads = build_uploads(Path(temp_dir))
        with httpx.Client(timeout=30.0) as client:
            health = client.get(f"{args.base}/health").json()
            print("health:", json.dumps(health, ensure_ascii=False))

            # 一个文件一个任务：接口只接受单个 file 字段；多传时只跑第一个
            name, _, data, mime = uploads[0]
            if len(uploads) > 1:
                print(f"注意：一次只能建一个任务，忽略其余 {len(uploads) - 1} 个文件")
            response = client.post(
                f"{args.base}{args.prefix}/analysis/tasks",
                files={"file": (name, data, mime)},
            )
            response.raise_for_status()
            task = response.json()
            task_id = task["task_id"]
            print("task:", task_id)

            # 先验证 SSE 进度流：回放历史事件并在任务结束时收到 done
            try:
                with client.stream(
                    "GET", f"{args.base}{args.prefix}/analysis/tasks/{task_id}/events"
                ) as stream:
                    event_type = "message"
                    for line in stream.iter_lines():
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                            continue
                        if not line.startswith("data: "):
                            continue
                        if event_type == "done":
                            break
                        event = json.loads(line[6:])
                        print(
                            f"  [{event['progress']:>3}%] {event['step']} - {event['message']}"
                        )
            except httpx.HTTPError as exc:
                print("SSE 订阅失败，改用轮询：", exc)

            deadline = time.time() + args.timeout
            while time.time() < deadline:
                snapshot = client.get(
                    f"{args.base}{args.prefix}/analysis/tasks/{task_id}"
                ).json()
                print(
                    f"  [{snapshot['progress']:>3}%] {snapshot['status']} - {snapshot['step']}"
                )
                if snapshot["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(1.0)
            else:
                print("超时未完成")
                return 1

            if snapshot["status"] != "succeeded":
                print("失败：", json.dumps(snapshot.get("error"), ensure_ascii=False))
                return 1

            result = client.get(
                f"{args.base}{args.prefix}/analysis/tasks/{task_id}/result"
            ).json()
            print(
                "summary:",
                json.dumps(result["summary"], ensure_ascii=False, indent=2),
            )
            print("master skipped:", result["master_bills"])
            print(
                "house bills:",
                [
                    {"no": bill["fields"].get("hawb"), "pages": bill["page_indexes"]}
                    for bill in result["house_bills"]
                ],
            )
            print(
                "mismatches:",
                [
                    {
                        "bill": item["hawb_no"],
                        "fields": [
                            field["key"]
                            for field in item["fields"]
                            if field["status"] == "mismatch"
                        ],
                    }
                    for item in result["comparisons"]
                    if item["status"] != "consistent"
                ],
            )
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
