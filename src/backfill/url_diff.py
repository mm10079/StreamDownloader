"""舊專案 format_url（get_formatinfo + guess.Finder）的通用化版本"""
import asyncio
import re
from typing import Optional

from .base import BackfillStrategy, Probe, UrlTemplate

_RUN = re.compile(r"\d+|\D+")

# 常見的非連續序號間距（舊專案 Deltas）：以毫秒時間戳命名的片段
COMMON_DELTAS: list[list[int]] = [
    [2000, 2001, 1999, 1967, 1968, 1966, 2564],     # IG
    [8000, 8001, 7999, 7967, 7968, 7966, 8564],     # REALITY
]


def build_template(first: str, second: str) -> Optional[UrlTemplate]:
    """比較兩個片段網址：只允許「同一組數字」在一處或多處不同（路徑、query 皆可）。
    例：.../index_1_01669.ts 與 .../index_1_01670.ts → .../index_1_{num}.ts, fill=5, space=1"""
    a, b = _RUN.findall(first), _RUN.findall(second)
    if len(a) != len(b):
        return None
    pair: Optional[tuple[int, int]] = None
    fill = 0
    out: list[str] = []
    for x, y in zip(a, b):
        if x == y:
            out.append(x.replace("{", "{{").replace("}", "}}"))
            continue
        if not (x.isdigit() and y.isdigit()):
            return None
        if pair is None:
            pair = (int(x), int(y))
        elif pair != (int(x), int(y)):
            return None         # 有兩組以上獨立變化的數字（例如簽章），無法回溯
        if x.startswith("0") and len(x) > 1:
            fill = max(fill, len(x))
        out.append("{num}")
    if pair is None or pair[0] == pair[1]:
        return None
    return UrlTemplate(pattern="".join(out), fill=fill, space=abs(pair[1] - pair[0]))


class UrlDiffBackfill(BackfillStrategy):
    def __init__(self, distance: int = 10000, batch: int = 300, max_rounds: int = 10000,
                 max_scan: Optional[int] = None):
        self.distance = distance        # 連續序號時每輪往回探測的距離
        self.batch = batch              # 非連續時一次並行探測的數量
        self.max_rounds = max_rounds
        self.max_scan = max_scan        # 猜測失敗時範圍掃描的上限；None 不限（HLS 時間戳命名需要掃描）

    def build(self, urls: list[str]) -> Optional[UrlTemplate]:
        for i in range(len(urls) - 1):
            tpl = build_template(urls[i], urls[i + 1])
            if tpl:
                return tpl
        return None

    async def discover(self, template: UrlTemplate, known: list[int], probe: Probe,
                       hints: Optional[list[int]] = None) -> list[int]:
        if not known:
            return []
        lowest = min(known)
        if template.space == 1 and not hints:
            start = await self._continuous_start(lowest, probe)
            return list(range(start, lowest))
        return await self._guess_chain(lowest, template.space, probe, list(hints or []))

    # ---------- 連續序號：二分搜尋下界 ----------
    async def _continuous_start(self, lowest: int, probe: Probe) -> int:
        """假設有效範圍為 [start, lowest] 且連續；分段二分搜尋 start"""
        upper = lowest      # 已知有效
        while upper > 0:
            lower = max(0, upper - self.distance)
            if await probe(lower):
                upper = lower   # 整段都有效，繼續往前
                continue
            lo, hi = lower, upper   # probe(lo) 無效、probe(hi) 有效
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if await probe(mid):
                    hi = mid
                else:
                    lo = mid
            return hi
        return 0

    # ---------- 非連續序號（時間戳）：猜測常見間距，再退回範圍掃描 ----------
    async def _first_valid(self, candidates: list[int], probe: Probe) -> Optional[int]:
        """並行探測，回傳最大的有效值（最接近目前值，避免跳過片段）"""
        if not candidates:
            return None
        results = await asyncio.gather(*(probe(n) for n in candidates))
        hits = [n for n, ok in zip(candidates, results) if ok]
        return max(hits) if hits else None

    async def _guess_chain(self, current: int, space: int, probe: Probe, hints: list[int]) -> list[int]:
        common = next((list(d) for d in COMMON_DELTAS if space in d), [space])
        deltas = list(dict.fromkeys(hints + common))
        found: list[int] = []
        for _ in range(self.max_rounds):
            hit = await self._first_valid([current - d for d in deltas if current - d >= 0], probe)
            if hit is None:
                if self.max_scan is not None and space * 3 // 2 > self.max_scan:
                    break       # 間距太大（例如高 timescale 的 $Time$），範圍掃描請求量過大，只依靠猜測
                window_low = max(0, current - space * 3 // 2)
                for start in range(current - 1, window_low - 1, -self.batch):
                    chunk = list(range(start, max(window_low - 1, start - self.batch), -1))
                    hit = await self._first_valid(chunk, probe)
                    if hit is not None:
                        break
                if hit is None:
                    break
                if current - hit not in deltas:
                    deltas.append(current - hit)    # 學習新的間距
            found.append(hit)
            current = hit
        return sorted(found)
