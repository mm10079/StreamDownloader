"""下載結果（API 的回傳值）"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import StreamKind


@dataclass
class StreamOutcome:
    title: str
    kind: StreamKind
    url: str
    ok: bool                            # 全部片段成功（且需要合併時已合併）
    output: Optional[Path] = None       # 最終檔案；未合併或失敗時為 None
    backup: Optional[Path] = None       # 片段與續傳資料所在資料夾
    failed: list[str] = field(default_factory=list)     # 失敗的片段網址
    error: Optional[str] = None


@dataclass
class AttachmentOutcome:
    path: Path
    ok: bool
    url: Optional[str] = None


@dataclass
class DownloadResult:
    url: str
    title: Optional[str] = None
    output_dir: Optional[Path] = None
    extractor: Optional[str] = None
    streams: list[StreamOutcome] = field(default_factory=list)
    attachments: list[AttachmentOutcome] = field(default_factory=list)
    error: Optional[str] = None         # 解析階段的錯誤（例如不支援的網址、登入失敗）

    @property
    def ok(self) -> bool:
        """沒有錯誤，有內容，且所有串流與附件都成功"""
        return (self.error is None and bool(self.streams or self.attachments)
                and all(s.ok for s in self.streams) and all(a.ok for a in self.attachments))

    @property
    def files(self) -> list[Path]:
        """所有成功產生的檔案（串流輸出與附件）"""
        return [s.output for s in self.streams if s.ok and s.output] + [a.path for a in self.attachments if a.ok]
