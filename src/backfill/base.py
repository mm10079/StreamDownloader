"""回溯（backfill）：找出播放清單 / MPD 視窗以外、伺服器上仍存在的較早片段。

流程
1. build(urls)    由已知片段網址推出「網址模板」（數字部分換成 {num}）與序號間距
2. discover(...)  以 probe（實際請求）搜尋比已知最小序號更早的有效序號
協定層只依賴這個介面：HLS 用 UrlDiffBackfill，DASH 之後可用 SegmentTemplate 的 $Number$/$Time$。
"""
import re
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional
from pydantic import BaseModel

Probe = Callable[[int], Awaitable[bool]]


class UrlTemplate(BaseModel):
    pattern: str        # 含 {num} 的完整網址；其餘大括號已跳脫
    fill: int = 0       # 補零位數，0 表示不補
    space: int = 1      # 相鄰片段的序號差

    def format(self, num: int) -> str:
        return self.pattern.format(num=str(num).zfill(self.fill) if self.fill else str(num))

    def extract(self, url: str) -> Optional[int]:
        """反向從網址取出序號；不符合模板回傳 None"""
        parts = self.pattern.replace("{{", "\x00").replace("}}", "\x01").split("{num}")
        regex = r"(\d+)".join(re.escape(p.replace("\x00", "{").replace("\x01", "}")) for p in parts)
        m = re.fullmatch(regex, url)
        if not m:
            return None
        values = {int(g) for g in m.groups()}
        return values.pop() if len(values) == 1 else None


class BackfillStrategy(ABC):
    @abstractmethod
    def build(self, urls: list[str]) -> Optional[UrlTemplate]:
        ...

    @abstractmethod
    async def discover(self, template: UrlTemplate, known: list[int], probe: Probe) -> list[int]:
        """回傳比 known 最小值更早的有效序號（遞增排序，不含已知）"""
        ...
