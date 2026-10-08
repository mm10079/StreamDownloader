"""流程中傳遞的純資料。

這裡的物件只存 session_id 之類的參照，不直接持有 Session / Fetcher / Browser，
因此整份 MediaJob 可以序列化，供斷點續傳或除錯檢視。
"""
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field


class StreamKind(str, Enum):
    HLS = "hls"
    DASH = "dash"
    FILE = "file"


class StreamSpec(BaseModel):
    """一條要下載的串流（一個視角 / 一個檔案）"""
    kind: StreamKind
    url: str
    title: str = "media"
    session_id: str
    quality: int = 0
    backfill: bool = True       # 提取器可針對網站關閉回溯
    extras: dict[str, Any] = Field(default_factory=dict)


class AttachmentKind(str, Enum):
    URL = "url"     # 下載網址內容
    JSON = "json"   # 直接寫入資料
    TEXT = "text"


class AttachmentSpec(BaseModel):
    kind: AttachmentKind
    path: Path                  # 相對於 job 輸出資料夾
    url: Optional[str] = None
    data: Any = None
    session_id: Optional[str] = None


class MediaJob(BaseModel):
    """提取器的輸出：一個網址解析後要做的所有事"""
    title: str = "media"
    output_dir: Optional[Path] = None   # 為 None 時使用 Options.output
    streams: list[StreamSpec] = Field(default_factory=list)
    attachments: list[AttachmentSpec] = Field(default_factory=list)
