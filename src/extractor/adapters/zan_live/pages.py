"""頁面解析（純函式，輸入 HTML / JSON，不做網路請求）"""
import json
import re
from datetime import datetime
from typing import Any, Optional, Type, TypeVar, Iterable

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import BaseModel

from .schema.item import ArtistDetail, GiftItem, LiveRoomMetas, TicketDetail

T = TypeVar("T", bound=BaseModel)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def raw_metas(soup: BeautifulSoup) -> dict[str, str]:
    result = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        key = tag.get("name") or tag.get("property")
        if key and tag.get("content") is not None:
            result[str(key)] = str(tag.get("content"))
    return result


def get_meta(soup: BeautifulSoup, name: str) -> Optional[str]:
    return raw_metas(soup).get(name) or None


def import_metas(model: Type[T], soup: BeautifulSoup) -> T:
    """依欄位 json_schema_extra['key'] 對應 <meta name/property>；JSON 陣列字串自動解析"""
    metas = raw_metas(soup)
    data: dict[str, Any] = {}
    for field_name, info in model.model_fields.items():
        key = (info.json_schema_extra or {}).get("key") if isinstance(info.json_schema_extra, dict) else None
        if not isinstance(key, str) or key not in metas:
            continue
        value: Any = metas[key].strip()
        if not value:
            continue        # 空內容視為沒有此欄位，使用預設值
        if value[0] in "[{" and value[-1] in "]}":
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass        # 只是剛好以括號開頭的文字（例如標題），保留原字串
        data[field_name] = value
    return model(**data)


def extract_artists(soup: BeautifulSoup) -> list[ArtistDetail]:
    for tag in soup.find_all("script"):
        text = tag.string or ""
        if "ticketGroupArtists" not in text:
            continue
        m = re.search(r"ticketGroupArtists\s*=\s*(\[.*?\]);", text, re.DOTALL)
        if m:
            try:
                return [ArtistDetail(**item) for item in json.loads(m.group(1))]
            except (json.JSONDecodeError, ValueError):
                return []
    return []


def top_title_images(soup: BeautifulSoup) -> list[str]:
    return [str(img.get("src")) for img in soup.find_all("img", class_="topTitle")
            if isinstance(img, Tag) and str(img.get("src", "")).startswith("http")]


def gifts_of(metas: LiveRoomMetas) -> dict[str, list[GiftItem]]:
    """所有禮物欄位，以 meta key 為名"""
    result = {}
    for field_name, info in LiveRoomMetas.model_fields.items():
        value = getattr(metas, field_name)
        if isinstance(value, list) and value and isinstance(value[0], GiftItem):
            result[(info.json_schema_extra or {}).get("key", field_name)] = value
    return result


def comment_urls(manifest_url: str, manifest: dict) -> list[str]:
    urls = []
    if isinstance(manifest.get("comments"), dict):
        urls.extend(manifest["comments"].keys())
    urls.append(manifest_url)
    if isinstance(manifest.get("others"), str):
        urls.append(manifest["others"])
    return urls


def find_csrf(html: str) -> Optional[str]:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html) or re.search(r'value="([^"]+)"\s+name="_csrf"', html)
    return m.group(1) if m else None


# ---------------- 票券選擇 ----------------

def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()


def usable_ticket(t: TicketDetail) -> bool:
    return all(v is not None for v in (t.liveId, t.openLiveDate, t.liveBeginDate, t.liveEndDate)) \
        and t.isShowStreamBeginDate


def select_tickets(
    tickets: Iterable[dict],
    now: datetime,
    ticket_id: Optional[str] = None,
    live_id: Optional[str] = None,
    skip: Iterable[str] = (),
    playroom_url=lambda tid, lid: "",
) -> list[TicketDetail]:
    """選票規則（同舊版）：
    1. 指定 ticket_id / live_id 時只看該票
    2. 有直播中的票 → 全部（多視角）
    3. 否則取「尚未開始」中開演時間最早的一組
    已過開演時間卻不是直播中 → 視為過期略過
    """
    skip = set(skip)
    live: list[TicketDetail] = []
    upcoming: list[TicketDetail] = []
    for raw in tickets:
        try:
            t = TicketDetail(**raw)
        except ValueError:
            continue
        if not usable_ticket(t):
            continue
        if ticket_id is not None and str(t.id) != str(ticket_id):
            continue
        if live_id is not None and str(t.liveId) != str(live_id):
            continue
        if {str(t.id), str(t.liveId), playroom_url(t.id, t.liveId)} & skip:
            continue
        if t.isLive:
            live.append(t)
        elif _parse_time(t.openLiveDate) > now:
            upcoming.append(t)
        elif ticket_id is not None:
            live.append(t)      # 明確指定的票：交給後續流程判斷（可能是剛結束的存檔）
    if live:
        return live
    if not upcoming:
        return []
    earliest = min(_parse_time(t.openLiveDate) for t in upcoming)
    return [t for t in upcoming if _parse_time(t.openLiveDate) == earliest]


def open_time(t: TicketDetail) -> datetime:
    return _parse_time(t.openLiveDate)
