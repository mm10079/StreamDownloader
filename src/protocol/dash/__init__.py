"""DASH（MPD）— 預留骨架

規劃
1. parser：解析 MPD → Period / AdaptationSet / Representation，
   支援 SegmentTemplate（$Number$ / $Time$ / SegmentTimeline）、SegmentList、SegmentBase。
2. 影像與音訊為不同 Representation：各自用一個 SegmentStore（backup/{title}/video、audio），
   最後由 postprocess 以 ffmpeg 合併兩軌。
3. 直播（type="dynamic"）：依 minimumUpdatePeriod 重新讀取 MPD，與 HLS 的 _monitor 相同模式。
4. 回溯：MPD 本身就提供模板，不需要 UrlDiffBackfill.build 推測。
   - $Number$：往 startNumber 以前探測，間距 1 → 可直接用 UrlDiffBackfill 的連續二分搜尋
   - $Time$  ：間距為片段長度（timescale 單位），對應舊專案 Deltas 的時間戳猜測
   做法是把 SegmentTemplate 轉成 backfill.UrlTemplate（{num}），再呼叫 strategy.discover()。
5. 加密（CENC / Widevine）不在範圍內，偵測到 ContentProtection 時應明確回報。
"""
from ...core.models import StreamKind
from ..base import StreamProtocol, StreamResult


class DashProtocol(StreamProtocol):
    kind = StreamKind.DASH

    async def run(self) -> StreamResult:
        raise NotImplementedError("DASH 下載尚未實作，請見 src/protocol/dash/__init__.py 的規劃")


__all__ = ["DashProtocol"]
