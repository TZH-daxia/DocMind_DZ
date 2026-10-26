"""Prompt 加载：每次现读文件，改 prompt 不需重启服务。"""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent


def _load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def load_page_transcription_prompt() -> str:
    return _load("page_transcription.md")


def load_house_zoom_prompt() -> str:
    return _load("house_zoom_transcription.md")


def load_segment_classification_prompt() -> str:
    return _load("segment_classification.md")


def load_house_bill_extraction_prompt() -> str:
    return _load("house_bill_extraction.md")


def load_manifest_extraction_prompt() -> str:
    return _load("manifest_extraction.md")
