"""片段下載狀態。以 key（序號）為鍵，定期寫成 JSON 以支援斷點續傳。"""
import json
import os
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional
from pydantic import BaseModel


class SegStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class KeyInfo(BaseModel):
    method: str = "NONE"            # NONE / AES-128 / SAMPLE-AES
    uri: str = ""
    iv: Optional[str] = None        # 32 位 hex，已補齊（播放清單未給時由序號推算）
    iv_explicit: bool = False       # IV 是否由播放清單明確給出
    local: str = ""                 # 下載後的本地檔名


class Segment(BaseModel):
    key: int                        # 排序鍵：媒體序號，或回溯模式下網址中的數字
    url: str
    filename: str
    duration: float = 0.0
    media_sequence: Optional[int] = None
    encryption: Optional[KeyInfo] = None
    init_url: Optional[str] = None  # EXT-X-MAP 網址
    init: Optional[str] = None      # EXT-X-MAP 本地檔名
    byte_range: Optional[tuple[int, int]] = None
    program_date_time: Optional[str] = None
    discontinuity: bool = False
    from_backfill: bool = False
    status: SegStatus = SegStatus.PENDING
    retries: int = 0
    size: int = 0


class StoreData(BaseModel):
    source_url: str = ""
    key_mode: str = ""              # "sequence"（媒體序號）或 "template"（網址數字）
    header_lines: list[str] = []    # 原始播放清單的標頭（VERSION 等）
    target_duration: float = 0
    segments: dict[int, Segment] = {}


class SegmentStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = StoreData()

    # ---------- 持久化 ----------
    @classmethod
    def load(cls, path: Path) -> "SegmentStore":
        store = cls(path)
        if store.path.is_file():
            store.data = StoreData.model_validate_json(store.path.read_text(encoding="utf-8"))
            for seg in store.data.segments.values():
                if seg.status is SegStatus.RUNNING:
                    seg.status = SegStatus.PENDING   # 上次中斷時下載到一半
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self.data.model_dump_json(indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 操作 ----------
    def __contains__(self, key: int) -> bool:
        return key in self.data.segments

    def __len__(self) -> int:
        return len(self.data.segments)

    def get(self, key: int) -> Optional[Segment]:
        return self.data.segments.get(key)

    def add(self, seg: Segment) -> bool:
        """新增片段；已存在時只在尚未完成的情況下更新網址（簽章網址可能換新）"""
        old = self.data.segments.get(seg.key)
        if old is None:
            self.data.segments[seg.key] = seg
            return True
        if old.status is not SegStatus.DONE and old.url != seg.url and not seg.from_backfill:
            old.url = seg.url
        return False

    def ordered(self) -> Iterator[Segment]:
        for key in sorted(self.data.segments):
            yield self.data.segments[key]

    def pending(self) -> list[Segment]:
        return [s for s in self.ordered() if s.status in (SegStatus.PENDING, SegStatus.FAILED)]

    def count(self, status: SegStatus) -> int:
        return sum(1 for s in self.data.segments.values() if s.status is status)

    @property
    def keys(self) -> list[int]:
        return sorted(self.data.segments)
