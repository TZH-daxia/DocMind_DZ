"""多格式解析器：把上传文件统一成「页面列表（文本 + 可选图片）」。

设计要点：
- 所有格式都产出 `ParsedDocument`，下游管线只认页面，不再关心文件后缀；
- PDF 同时保留文本层与页面图片：有可靠文本层时直接用文本（快且准），
  扫描件/文本层缺失时由管线调用 VLM 转写；
- Word/Excel 直接抽结构化文本（表格按行输出），不依赖 LibreOffice；
- .doc/.xls 旧格式在安装了 LibreOffice 时先转 PDF，否则明确报错。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.config import Settings

logger = logging.getLogger(__name__)

# Word / Excel 无分页概念，按行数切成"逻辑页"，保证单页内容量可控
DOCX_LINES_PER_PAGE = 55
SHEET_ROWS_PER_PAGE = 80


class ParserError(RuntimeError):
    """解析失败，message 为面向前端的中文原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ParsedPage:
    index: int
    text: str = ""
    text_source: str = "empty"
    image_path: Path | None = None


@dataclass
class ParsedDocument:
    pages: list[ParsedPage] = field(default_factory=list)
    parser: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)


class DocumentParser(Protocol):
    name: str
    extensions: tuple[str, ...]

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument: ...


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    cleaned: list[str] = []
    for line in lines:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _paginate_lines(lines: list[str], per_page: int) -> list[str]:
    pages: list[str] = []
    for start in range(0, max(len(lines), 1), per_page):
        chunk = _normalize_text("\n".join(lines[start : start + per_page]))
        if chunk:
            pages.append(chunk)
    return pages or [""]


class PdfParser:
    name = "pdf"
    extensions = (".pdf",)

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument:
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise ParserError("PARSER_UNAVAILABLE", "缺少 PyMuPDF 依赖，无法解析 PDF") from exc

        document = ParsedDocument(parser="pdf")
        zoom = settings.render_dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        try:
            with pymupdf.open(source) as pdf:
                total = pdf.page_count
                if total > settings.max_pages:
                    document.warnings.append(
                        f"文件共 {total} 页，仅解析前 {settings.max_pages} 页"
                    )
                for offset in range(min(total, settings.max_pages)):
                    page = pdf.load_page(offset)
                    text = _normalize_text(page.get_text("text"))
                    image_path = out_dir / f"page_{offset + 1:03d}.png"
                    try:
                        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                        pixmap.save(image_path)
                    except Exception as exc:  # noqa: BLE001 - 单页渲染失败不阻断
                        logger.warning("第 %s 页渲染失败：%s", offset + 1, exc)
                        image_path = None
                    document.pages.append(
                        ParsedPage(
                            index=offset + 1,
                            text=text,
                            text_source="text_layer" if text else "empty",
                            image_path=image_path,
                        )
                    )
        except ParserError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ParserError("PARSE_FAILED", f"PDF 解析失败：{exc}") from exc
        return document


class DocxParser:
    name = "docx"
    extensions = (".docx",)

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument:
        try:
            from docx import Document as WordDocument
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError as exc:  # pragma: no cover
            raise ParserError("PARSER_UNAVAILABLE", "缺少 python-docx 依赖") from exc

        document = ParsedDocument(parser="docx")
        try:
            word = WordDocument(str(source))
            lines: list[str] = []
            for child in word.element.body.iterchildren():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    text = Paragraph(child, word).text.strip()
                    if text:
                        lines.append(text)
                elif tag == "tbl":
                    table = Table(child, word)
                    for row in table.rows:
                        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                        if any(cells):
                            lines.append(" | ".join(cells))
            for index, text in enumerate(_paginate_lines(lines, DOCX_LINES_PER_PAGE), start=1):
                document.pages.append(
                    ParsedPage(index=index, text=text, text_source="docx")
                )
            if not document.pages:
                document.pages.append(ParsedPage(index=1, text="", text_source="empty"))
        except ParserError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ParserError("PARSE_FAILED", f"Word 解析失败：{exc}") from exc
        return document


class ExcelParser:
    name = "xlsx"
    extensions = (".xlsx",)

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise ParserError("PARSER_UNAVAILABLE", "缺少 openpyxl 依赖") from exc

        document = ParsedDocument(parser="xlsx")
        try:
            workbook = load_workbook(source, read_only=True, data_only=True)
            page_index = 0
            for sheet in workbook.worksheets:
                lines = [f"# Sheet: {sheet.title}"]
                for row in sheet.iter_rows(values_only=True):
                    cells = ["" if value is None else str(value).strip() for value in row]
                    while cells and not cells[-1]:
                        cells.pop()
                    if any(cells):
                        lines.append(" | ".join(cells))
                for chunk in _paginate_lines(lines, SHEET_ROWS_PER_PAGE):
                    page_index += 1
                    document.pages.append(
                        ParsedPage(index=page_index, text=chunk, text_source="xlsx")
                    )
            workbook.close()
            if not document.pages:
                document.pages.append(ParsedPage(index=1, text="", text_source="empty"))
        except Exception as exc:  # noqa: BLE001
            raise ParserError("PARSE_FAILED", f"Excel 解析失败：{exc}") from exc
        return document


class XlsParser:
    name = "xls"
    extensions = (".xls",)

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument:
        try:
            import xlrd
        except ImportError as exc:  # pragma: no cover
            raise ParserError("PARSER_UNAVAILABLE", "缺少 xlrd 依赖") from exc

        document = ParsedDocument(parser="xls")
        try:
            book = xlrd.open_workbook(str(source))
            page_index = 0
            for sheet in book.sheets():
                lines = [f"# Sheet: {sheet.name}"]
                for row_index in range(sheet.nrows):
                    cells = [str(value).strip() for value in sheet.row_values(row_index)]
                    while cells and not cells[-1]:
                        cells.pop()
                    if any(cells):
                        lines.append(" | ".join(cells))
                for chunk in _paginate_lines(lines, SHEET_ROWS_PER_PAGE):
                    page_index += 1
                    document.pages.append(
                        ParsedPage(index=page_index, text=chunk, text_source="xlsx")
                    )
            if not document.pages:
                document.pages.append(ParsedPage(index=1, text="", text_source="empty"))
        except Exception as exc:  # noqa: BLE001
            raise ParserError("PARSE_FAILED", f"Excel(xls) 解析失败：{exc}") from exc
        return document


class ImageParser:
    name = "image"
    extensions = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise ParserError("PARSER_UNAVAILABLE", "缺少 Pillow 依赖") from exc

        document = ParsedDocument(parser="image")
        try:
            with Image.open(source) as image:
                frames = getattr(image, "n_frames", 1)
                for frame_index in range(min(frames, settings.max_pages)):
                    if frames > 1:
                        image.seek(frame_index)
                    target = out_dir / f"page_{frame_index + 1:03d}.png"
                    converted = image.convert("RGB")
                    # 限制超长边，避免 1200 万像素手机照片直接喂模型
                    converted.thumbnail((2600, 2600))
                    converted.save(target, "PNG")
                    document.pages.append(
                        ParsedPage(index=frame_index + 1, text="", image_path=target)
                    )
                if frames > settings.max_pages:
                    document.warnings.append(
                        f"文件共 {frames} 页，仅解析前 {settings.max_pages} 页"
                    )
        except Exception as exc:  # noqa: BLE001
            raise ParserError("PARSE_FAILED", f"图片解析失败：{exc}") from exc
        return document


class LegacyOfficeParser:
    """`.doc` / `.xls`：借助 LibreOffice 转 PDF 后复用 PDF 解析器。"""

    name = "legacy_office"
    extensions = (".doc", ".xls")

    def parse(self, source: Path, out_dir: Path, settings: Settings) -> ParsedDocument:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            raise ParserError(
                "LEGACY_FORMAT_UNSUPPORTED",
                "旧版 .doc/.xls 需要安装 LibreOffice 才能解析，请另存为 .docx/.xlsx 后重试",
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            command = [
                soffice,
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                temp_dir,
                str(source),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=120)
            except (subprocess.SubprocessError, OSError) as exc:
                raise ParserError("PARSE_FAILED", f"LibreOffice 转换失败：{exc}") from exc
            pdf_path = Path(temp_dir) / f"{source.stem}.pdf"
            if not pdf_path.exists():
                raise ParserError("PARSE_FAILED", "LibreOffice 未生成 PDF")
            return PdfParser().parse(pdf_path, out_dir, settings)


class ParserRegistry:
    """后缀 → 解析器注册表（新增格式只需 register）。"""

    def __init__(self) -> None:
        self._parsers: dict[str, DocumentParser] = {}

    def register(self, parser: DocumentParser) -> None:
        for extension in parser.extensions:
            self._parsers[extension.lower()] = parser

    def get(self, suffix: str) -> DocumentParser:
        parser = self._parsers.get(suffix.lower())
        if parser is None:
            raise ParserError("FILE_TYPE_NOT_SUPPORTED", f"暂不支持的文件类型：{suffix}")
        return parser

    def supports(self, suffix: str) -> bool:
        return suffix.lower() in self._parsers

    @property
    def extensions(self) -> tuple[str, ...]:
        return tuple(sorted(self._parsers))


def build_default_registry() -> ParserRegistry:
    registry = ParserRegistry()
    # 顺序有意义：.doc/.xls 必须由 LegacyOfficeParser 处理
    registry.register(LegacyOfficeParser())
    registry.register(PdfParser())
    registry.register(DocxParser())
    registry.register(ExcelParser())
    registry.register(XlsParser())
    registry.register(ImageParser())
    return registry
