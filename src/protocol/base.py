"""Protocol：理解某種串流格式，產生片段並交給 Fetcher 下載。"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core.models import StreamKind, StreamSpec
from ..core.options import Options
from ..fetcher import Fetcher
from ..session import SessionManager


@dataclass
class StreamContext:
    stream: StreamSpec
    options: Options
    sessions: SessionManager
    fetcher: Fetcher
    output_dir: Path
    stop: asyncio.Event = field(default_factory=asyncio.Event)   # 軟停止：不再追新片段，完成進行中的下載


@dataclass
class StreamResult:
    complete: bool                          # 所有片段皆成功
    output: Optional[Path] = None           # 已是最終檔案（FILE）或合併目標路徑
    playlist: Optional[Path] = None         # 待合併的本地播放清單
    failed: list[str] = field(default_factory=list)


class StreamError(Exception):
    pass


class StreamProtocol(ABC):
    kind: StreamKind

    def __init__(self, ctx: StreamContext):
        self.ctx = ctx

    @property
    def session(self):
        return self.ctx.sessions.get(self.ctx.stream.session_id)

    @abstractmethod
    async def run(self) -> StreamResult:
        ...


def protocol_for(kind: StreamKind) -> type[StreamProtocol]:
    if kind is StreamKind.HLS:
        from .hls import HlsProtocol
        return HlsProtocol
    if kind is StreamKind.DASH:
        from .dash import DashProtocol
        return DashProtocol
    if kind is StreamKind.FILE:
        from .file import FileProtocol
        return FileProtocol
    raise ValueError(f"不支援的串流類型: {kind}")
