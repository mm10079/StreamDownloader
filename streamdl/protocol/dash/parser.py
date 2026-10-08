"""MPD 解析（純函式，不做網路請求；需要「現在時間」的計算由呼叫者傳入 now）

支援
- BaseURL 逐層解析（MPD → Period → AdaptationSet → Representation）
- SegmentTemplate：$RepresentationID$ / $Number$ / $Time$ / $Bandwidth$（含 %0Nd 格式）、
  SegmentTimeline（t / d / r，r=-1）、以 duration 推算（static 依總長，dynamic 依現在時間與 timeShiftBufferDepth）
- SegmentList（SegmentURL / mediaRange / Initialization）
- SegmentBase / 單一檔案：整個檔案視為一個片段
- AdaptationSet 層級的 SegmentTemplate 由 Representation 繼承並覆寫
"""
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

from ...backfill import UrlTemplate

NS = {"d": "urn:mpeg:dash:schema:mpd:2011"}
CENC_NS = "urn:mpeg:cenc:2013"
DRM_SYSTEMS = {
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Widevine",
    "9a04f079-9840-4286-ab92-e65be0885f95": "PlayReady",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "FairPlay",
    "e2719d58-a985-b3c9-781a-b030af78d30e": "ClearKey",
    "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b": "ClearKey",
}
_DURATION = re.compile(
    r"^P(?:(?P<y>\d+(?:\.\d+)?)Y)?(?:(?P<mo>\d+(?:\.\d+)?)M)?(?:(?P<w>\d+(?:\.\d+)?)W)?(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$")
_IDENT = re.compile(r"\$(RepresentationID|Number|Bandwidth|Time|SubNumber)(?:%0(\d+)d)?\$")
DYNAMIC_EDGE_MARGIN = 2     # 以時間推算的直播片段，避開仍在產生中的最新片段


class MpdError(Exception):
    pass


# ---------------- 基本工具 ----------------

def parse_duration(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = _DURATION.match(text.strip())
    if not m:
        raise MpdError(f"無法解析的時間長度：{text}")
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return (g["y"] * 365 + g["mo"] * 30 + g["w"] * 7 + g["d"]) * 86400 + g["h"] * 3600 + g["m"] * 60 + g["s"]


def parse_datetime(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fill_template(template: str, rep_id: str = "", bandwidth: int = 0,
                  number: Optional[int] = None, time: Optional[int] = None) -> str:
    def sub(m: re.Match) -> str:
        name, width = m.group(1), m.group(2)
        value = {"RepresentationID": rep_id, "Bandwidth": bandwidth, "Number": number,
                 "Time": time, "SubNumber": None}[name]
        if value is None:
            return m.group(0)
        text = str(value)
        return text.zfill(int(width)) if width else text
    return _IDENT.sub(sub, template).replace("$$", "$")


def _children(el: Optional[ET.Element], tag: str) -> list[ET.Element]:
    return [] if el is None else el.findall(f"d:{tag}", NS)


def _child(el: Optional[ET.Element], tag: str) -> Optional[ET.Element]:
    return None if el is None else el.find(f"d:{tag}", NS)


def _base(url: str, el: Optional[ET.Element]) -> str:
    b = _child(el, "BaseURL")
    return urljoin(url, b.text.strip()) if b is not None and b.text else url


# ---------------- 資料結構 ----------------

@dataclass
class DashSegment:
    key: int                    # $Number$ 或 $Time$（依 media 模板使用何者）
    url: str
    duration: float = 0.0       # 秒
    byte_range: Optional[tuple[int, int]] = None


@dataclass
class Representation:
    id: str
    content_type: str           # video / audio / text
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    lang: str = ""
    mime_type: str = ""
    init_url: Optional[str] = None
    init_range: Optional[tuple[int, int]] = None
    segments: list[DashSegment] = field(default_factory=list)
    url_template: Optional[UrlTemplate] = None      # 可回溯時的 {num} 模板
    key_kind: str = "number"                        # number / time / index
    durations: list[int] = field(default_factory=list)   # 片段長度（key 單位），供回溯猜測間距
    time_origin: int = 0                                 # $Time$ 的起點（presentationTimeOffset）
    protected: bool = False                             # 有 ContentProtection（CENC 加密）
    drm_systems: list[str] = field(default_factory=list)  # Widevine / PlayReady / ClearKey …
    default_kid: Optional[str] = None                   # 32 位 hex，無連字號
    clearkey_laurl: Optional[str] = None                # ClearKey 授權伺服器

    @property
    def label(self) -> str:
        if self.content_type == "video":
            return f"{self.height}p {self.bandwidth // 1000}kbps" if self.height else f"{self.bandwidth // 1000}kbps"
        return f"{self.content_type} {self.lang} {self.bandwidth // 1000}kbps".replace("  ", " ")


@dataclass
class Manifest:
    url: str
    dynamic: bool
    minimum_update_period: Optional[float]
    representations: list[Representation]

    def by_type(self, content_type: str) -> list[Representation]:
        reps = [r for r in self.representations if r.content_type == content_type]
        return sorted(reps, key=lambda r: r.bandwidth, reverse=True)

    def find(self, rep_id: str, content_type: str) -> Optional[Representation]:
        return next((r for r in self.representations if r.id == rep_id and r.content_type == content_type), None)


# ---------------- 解析 ----------------

def _content_protection(rep: "Representation", elements: list[ET.Element], base: str) -> None:
    for cp in elements:
        rep.protected = True
        kid = cp.get(f"{{{CENC_NS}}}default_KID")
        if kid:
            rep.default_kid = kid.replace("-", "").lower()
        scheme = (cp.get("schemeIdUri") or "").lower()
        if scheme.startswith("urn:uuid:"):
            name = DRM_SYSTEMS.get(scheme[9:], scheme[9:])
            if name not in rep.drm_systems:
                rep.drm_systems.append(name)
            if name == "ClearKey":
                for child in cp.iter():
                    if child.tag.lower().endswith("laurl") and child.text:
                        rep.clearkey_laurl = urljoin(base, child.text.strip())   # 可能是相對路徑


def _range(text: Optional[str]) -> Optional[tuple[int, int]]:
    if not text or "-" not in text:
        return None
    a, b = text.split("-", 1)
    return int(a), int(b)


def _content_type(aset: ET.Element, rep: ET.Element) -> str:
    ct = aset.get("contentType") or ""
    mime = rep.get("mimeType") or aset.get("mimeType") or ""
    if not ct:
        ct = mime.split("/")[0] if mime else ""
    if ct == "application" or "ttml" in mime or "vtt" in mime:
        ct = "text"
    return ct or "video"


def _merged_template(aset: ET.Element, rep: ET.Element, period: ET.Element) -> Optional[dict]:
    """依 Period → AdaptationSet → Representation 合併 SegmentTemplate 屬性與 SegmentTimeline"""
    merged: dict = {}
    timeline = None
    found = False
    for el in (period, aset, rep):
        t = _child(el, "SegmentTemplate")
        if t is None:
            continue
        found = True
        merged.update(t.attrib)
        tl = _child(t, "SegmentTimeline")
        if tl is not None:
            timeline = tl
    if not found:
        return None
    merged["_timeline"] = timeline
    return merged


def _template_segments(tpl: dict, base: str, rep_id: str, bandwidth: int, period_start: float,
                       period_duration: Optional[float], dynamic: bool, ast: Optional[datetime],
                       tsbd: Optional[float], now: datetime) -> tuple[list[DashSegment], str, list[int]]:
    media = tpl.get("media", "")
    timescale = int(tpl.get("timescale", 1))
    start_number = int(tpl.get("startNumber", 1))
    pto = int(tpl.get("presentationTimeOffset", 0))
    uses_time = "$Time" in media
    segs: list[DashSegment] = []
    durations: list[int] = []

    def add(number: int, t: int, d: int):
        url = urljoin(base, fill_template(media, rep_id, bandwidth, number=number, time=t))
        segs.append(DashSegment(key=t if uses_time else number, url=url, duration=d / timescale))
        durations.append(d)

    timeline = tpl.get("_timeline")
    if timeline is not None:
        number = start_number
        t = 0
        entries = _children(timeline, "S")
        for i, s in enumerate(entries):
            if s.get("t") is not None:
                t = int(s.get("t"))
            d = int(s.get("d"))
            r = int(s.get("r", 0))
            if r < 0:
                # 重複到下一個 S 的起點，或 Period 結束（dynamic 則為現在）
                nxt = entries[i + 1].get("t") if i + 1 < len(entries) else None
                if nxt is not None:
                    end = int(nxt)
                elif period_duration is not None:
                    end = pto + int(period_duration * timescale)
                elif dynamic and ast is not None:
                    end = pto + int(((now - ast).total_seconds() - period_start) * timescale)
                else:
                    end = t + d
                r = max(0, math.ceil((end - t) / d) - 1)
            for _ in range(r + 1):
                add(number, t, d)
                number += 1
                t += d
    else:
        dur = int(tpl.get("duration", 0))
        if not dur:
            raise MpdError("SegmentTemplate 缺少 duration 與 SegmentTimeline")
        seg_seconds = dur / timescale
        if dynamic:
            if ast is None:
                raise MpdError("動態 MPD 缺少 availabilityStartTime")
            elapsed = (now - ast).total_seconds() - period_start
            latest = start_number + math.floor(elapsed / seg_seconds) - 1 - DYNAMIC_EDGE_MARGIN
            window = math.floor(tsbd / seg_seconds) if tsbd else 5
            first = max(start_number, latest - window + 1)
        else:
            if period_duration is None:
                raise MpdError("無法得知 Period 長度")
            first = start_number
            latest = start_number + math.ceil(period_duration / seg_seconds - 1e-9) - 1
        for number in range(first, latest + 1):
            add(number, pto + (number - start_number) * dur, dur)

    return segs, ("time" if uses_time else "number"), durations


def to_url_template(media: str, base: str, rep_id: str, bandwidth: int) -> Optional[UrlTemplate]:
    """把 SegmentTemplate 的 media 轉成回溯用的 {num} 模板（只能有 $Number$ 或 $Time$ 其中一種）"""
    names = {m.group(1) for m in _IDENT.finditer(media)}
    if ("Number" in names) == ("Time" in names) or "SubNumber" in names:
        return None
    var = "Number" if "Number" in names else "Time"
    fill = 0
    marker = "STREAMDLNUMMARKER"

    def sub(m: re.Match) -> str:
        nonlocal fill
        if m.group(1) == var:
            fill = max(fill, int(m.group(2) or 0))
            return marker
        return m.group(0)

    url = urljoin(base, fill_template(_IDENT.sub(sub, media), rep_id, bandwidth))
    pattern = url.replace("{", "{{").replace("}", "}}").replace(marker, "{num}")
    return UrlTemplate(pattern=pattern, fill=fill, space=1)


def parse_mpd(text: str, url: str, now: Optional[datetime] = None) -> Manifest:
    now = now or datetime.now(timezone.utc)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise MpdError(f"MPD 不是合法的 XML：{e}") from e
    if not root.tag.endswith("MPD"):
        raise MpdError("不是 MPD 檔案")

    dynamic = root.get("type", "static") == "dynamic"
    ast = parse_datetime(root.get("availabilityStartTime"))
    tsbd = parse_duration(root.get("timeShiftBufferDepth"))
    total = parse_duration(root.get("mediaPresentationDuration"))
    mpd_base = _base(url, root)

    periods = _children(root, "Period")
    if not periods:
        raise MpdError("MPD 沒有 Period")
    # 動態 MPD 只取目前（最後一個）Period；靜態 MPD 取第一個（多 Period 尚未支援串接）
    period = periods[-1] if dynamic else periods[0]
    idx = periods.index(period)
    period_start = parse_duration(period.get("start")) or 0.0
    period_duration = parse_duration(period.get("duration"))
    if period_duration is None and not dynamic:
        nxt = periods[idx + 1].get("start") if idx + 1 < len(periods) else None
        if nxt is not None:
            period_duration = parse_duration(nxt) - period_start
        elif total is not None:
            period_duration = total - period_start
    period_base = _base(mpd_base, period)

    reps: list[Representation] = []
    for aset in _children(period, "AdaptationSet"):
        aset_base = _base(period_base, aset)
        for rep_el in _children(aset, "Representation"):
            rep_id = rep_el.get("id", "")
            bandwidth = int(rep_el.get("bandwidth", 0) or 0)
            base = _base(aset_base, rep_el)
            rep = Representation(
                id=rep_id, content_type=_content_type(aset, rep_el), bandwidth=bandwidth,
                width=int(rep_el.get("width") or aset.get("width") or 0),
                height=int(rep_el.get("height") or aset.get("height") or 0),
                codecs=rep_el.get("codecs") or aset.get("codecs") or "",
                lang=aset.get("lang", ""), mime_type=rep_el.get("mimeType") or aset.get("mimeType") or "",
            )
            _content_protection(rep, _children(aset, "ContentProtection") + _children(rep_el, "ContentProtection"), url)
            tpl = _merged_template(aset, rep_el, period)
            seg_list = _child(rep_el, "SegmentList")
            if seg_list is None:        # 注意：沒有子元素的 Element 為 falsy，不能用 or
                seg_list = _child(aset, "SegmentList")
            if tpl is not None:
                if tpl.get("initialization"):
                    rep.init_url = urljoin(base, fill_template(tpl["initialization"], rep_id, bandwidth))
                rep.segments, rep.key_kind, rep.durations = _template_segments(
                    tpl, base, rep_id, bandwidth, period_start, period_duration, dynamic, ast, tsbd, now)
                rep.url_template = to_url_template(tpl.get("media", ""), base, rep_id, bandwidth)
                rep.time_origin = int(tpl.get("presentationTimeOffset", 0))
            elif seg_list is not None:
                init = _child(seg_list, "Initialization")
                if init is not None:
                    rep.init_url = urljoin(base, init.get("sourceURL")) if init.get("sourceURL") else base
                    rep.init_range = _range(init.get("range"))
                timescale = int(seg_list.get("timescale", 1))
                dur = int(seg_list.get("duration", 0)) / timescale
                start = int(seg_list.get("startNumber", 1))
                for i, s in enumerate(_children(seg_list, "SegmentURL")):
                    rep.segments.append(DashSegment(
                        key=start + i, url=urljoin(base, s.get("media")) if s.get("media") else base,
                        duration=dur, byte_range=_range(s.get("mediaRange"))))
                rep.key_kind = "number"
            else:
                # SegmentBase 或單一檔案
                rep.segments = [DashSegment(key=0, url=base, duration=period_duration or 0)]
                rep.key_kind = "index"
            reps.append(rep)

    mup = parse_duration(root.get("minimumUpdatePeriod"))
    return Manifest(url=url, dynamic=dynamic, minimum_update_period=mup, representations=reps)
