"""上传载体模型。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class UploadedDocument:
    filename: str
    content: bytes
    content_type: str | None = None

    @property
    def size(self) -> int:
        return len(self.content)
