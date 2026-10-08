"""M3U8 解析（純函式，不做網路請求）"""
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(text: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


@dataclass
class Variant:
    uri: str
    bandwidth: int = 0
    resolution: str = ""
    name: str = ""
    audio_group: str = ""
    line: str = ""


@dataclass
class Rendition:
    type: str
    group_id: str
    uri: str = ""
    name: str = ""
    default: bool = False


@dataclass
class MasterPlaylist:
    url: str
    variants: list[Variant] = field(default_factory=list)       # 依頻寬由高到低
    renditions: list[Rendition] = field(default_factory=list)


@dataclass
class Key:
    method: str = "NONE"
    uri: str = ""
    iv: Optional[str] = None        # 播放清單原始值（hex，無 0x）
    keyformat: str = "identity"

    @property
    def encrypted(self) -> bool:
        return self.method not in ("NONE", "")

    @property
    def drm(self) -> bool:
        """金鑰只交給授權解密模組（FairPlay / Widevine / PlayReady）→ 無法下載解密"""
        return self.encrypted and (
            self.method in ("SAMPLE-AES-CTR", "SAMPLE-AES-CENC")
            or self.keyformat.lower() not in ("", "identity")
            or not self.uri.lower().startswith(("http://", "https://")))

    @property
    def drm_name(self) -> str:
        fmt, uri = self.keyformat.lower(), self.uri.lower()
        if uri.startswith("skd:") or "streamingkeydelivery" in fmt:
            return "FairPlay"
        if "edef8ba9" in fmt:
            return "Widevine"
        if "9a04f079" in fmt or "playready" in fmt:
            return "PlayReady"
        return self.keyformat or self.method


def choose_key(keys: list[Key]) -> Optional[Key]:
    """同一組片段可能同時列出多個 EXT-X-KEY（不同 KEYFORMAT）：優先可直接下載金鑰的 identity"""
    usable = [k for k in keys if k.encrypted and not k.drm]
    if usable:
        return usable[0]
    return next((k for k in keys if k.encrypted), None)


@dataclass
class HlsSegment:
    uri: str                        # 已解析為絕對網址
    raw_uri: str
    duration: float
    media_sequence: int
    key: Optional[Key] = None
    map_uri: Optional[str] = None
    byte_range: Optional[tuple[int, int]] = None
    program_date_time: Optional[str] = None
    discontinuity: bool = False


@dataclass
class MediaPlaylist:
    url: str
    target_duration: float = 0
    media_sequence: int = 0
    playlist_type: str = ""
    endlist: bool = False
    header_lines: list[str] = field(default_factory=list)
    segments: list[HlsSegment] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return not self.endlist and self.playlist_type != "VOD"


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, url: str) -> MasterPlaylist:
    master = MasterPlaylist(url=url)
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            uri = next((l for l in lines[i + 1:] if l and not l.startswith("#")), "")
            master.variants.append(Variant(
                uri=urljoin(url, uri),
                bandwidth=int(attrs.get("BANDWIDTH", 0) or 0),
                resolution=attrs.get("RESOLUTION", ""),
                name=attrs.get("DISPLAY-NAME", "") or attrs.get("NAME", ""),
                audio_group=attrs.get("AUDIO", ""),
                line=line,
            ))
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            master.renditions.append(Rendition(
                type=attrs.get("TYPE", ""),
                group_id=attrs.get("GROUP-ID", ""),
                uri=urljoin(url, attrs["URI"]) if attrs.get("URI") else "",
                name=attrs.get("NAME", ""),
                default=attrs.get("DEFAULT") == "YES",
            ))
    master.variants.sort(key=lambda v: v.bandwidth, reverse=True)
    return master


def parse_media(text: str, url: str) -> MediaPlaylist:
    pl = MediaPlaylist(url=url)
    key: Optional[Key] = None
    key_group: list[Key] = []
    key_group_open = False
    map_uri: Optional[str] = None
    duration = 0.0
    pdt: Optional[str] = None
    disc = False
    byte_range: Optional[tuple[int, int]] = None
    last_range_end = 0
    seq_offset = 0
    seen_segment = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            tag, _, value = line.partition(":")
            if tag == "#EXTINF":
                duration = float(value.split(",")[0] or 0)
            elif tag == "#EXT-X-TARGETDURATION":
                pl.target_duration = float(value)
            elif tag == "#EXT-X-MEDIA-SEQUENCE":
                pl.media_sequence = int(value)
            elif tag == "#EXT-X-PLAYLIST-TYPE":
                pl.playlist_type = value.strip()
            elif tag == "#EXT-X-ENDLIST":
                pl.endlist = True
            elif tag == "#EXT-X-KEY":
                attrs = parse_attrs(value)
                iv = attrs.get("IV")
                raw_uri = attrs.get("URI", "")
                new = Key(
                    method=attrs.get("METHOD", "NONE"),
                    uri=urljoin(url, raw_uri) if raw_uri.startswith(("http", "/", ".")) or "://" not in raw_uri
                    else raw_uri,           # skd:// data: 等不做網址解析
                    iv=iv[2:] if iv and iv.lower().startswith("0x") else iv,
                    keyformat=attrs.get("KEYFORMAT", "identity"),
                )
                if new.method == "NONE":
                    key_group = []
                elif key_group_open:
                    key_group.append(new)       # 連續的 EXT-X-KEY 屬於同一組
                else:
                    key_group = [new]
                key_group_open = True
                key = choose_key(key_group)
            elif tag == "#EXT-X-MAP":
                attrs = parse_attrs(value)
                map_uri = urljoin(url, attrs["URI"]) if attrs.get("URI") else None
            elif tag == "#EXT-X-PROGRAM-DATE-TIME":
                pdt = value
            elif tag == "#EXT-X-DISCONTINUITY":
                disc = True
            elif tag == "#EXT-X-BYTERANGE":
                length, _, offset = value.partition("@")
                start = int(offset) if offset else last_range_end
                byte_range = (start, start + int(length) - 1)
                last_range_end = start + int(length)
            elif not seen_segment and tag not in ("#EXTM3U",):
                pl.header_lines.append(line)
            continue

        seen_segment = True
        key_group_open = False
        pl.segments.append(HlsSegment(
            uri=urljoin(url, line), raw_uri=line, duration=duration,
            media_sequence=pl.media_sequence + seq_offset,
            key=key if key and key.encrypted else None,
            map_uri=map_uri, byte_range=byte_range,
            program_date_time=pdt, discontinuity=disc,
        ))
        seq_offset += 1
        duration, pdt, disc, byte_range = 0.0, None, False, None

    # 標頭只保留不影響本地重建的資訊
    pl.header_lines = [l for l in pl.header_lines
                       if l.split(":")[0] in ("#EXT-X-VERSION", "#EXT-X-INDEPENDENT-SEGMENTS")]
    return pl


def legacy_base_candidates(playlist_url: str, raw_uri: str) -> list[str]:
    """舊專案 get_patch_url 的啟發式：部分網站的相對路徑與 RFC 解析結果不同，
    逐層移除播放清單路徑，產生可能的片段網址供探測（第一個為標準 urljoin）"""
    candidates = [urljoin(playlist_url, raw_uri)]
    if raw_uri.startswith("http"):
        return candidates
    scheme, rest = playlist_url.split("://", 1)
    parts = rest.split("?")[0].split("/")[:-1]
    for cut in range(len(parts) - 1, 0, -1):
        base = f"{scheme}://" + "/".join(parts[:cut]) + "/"
        url = base + raw_uri.lstrip("/")
        if url not in candidates:
            candidates.append(url)
    return candidates
