"""頁面 / API 解析（純函式，不做網路請求）"""
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup
from bs4.element import Tag

# 進入直播間卻被導回資訊頁時顯示的區域限制訊息（各語系）
REGION_NOTICES = ("日本国内", "日本國內", "日本境外", "日本国外", "outside japan", "only available in japan")


class SingularError(Exception):
    pass


# ---------------- 資訊頁 ----------------

def parse_event(html: str) -> Optional[dict]:
    """<div id="react-root" data-event="..."> 的 JSON；沒有則回傳 None"""
    root = BeautifulSoup(html, "html.parser").find("div", id="react-root")
    if not isinstance(root, Tag) or not root.get("data-event"):
        return None
    try:
        return json.loads(str(root["data-event"]))      # bs4 已處理 HTML 實體
    except json.JSONDecodeError as e:
        raise SingularError(f"data-event 不是合法的 JSON：{e}") from e


def og(html: str, prop: str) -> Optional[str]:
    tag = BeautifulSoup(html, "html.parser").find("meta", attrs={"property": f"og:{prop}"})
    return str(tag["content"]) if isinstance(tag, Tag) and tag.get("content") else None


def event_title(event: dict, html: str = "") -> str:
    loc = event.get("active_localization") or event.get("localization_ja") or {}
    if loc.get("title"):
        return loc["title"]
    title = og(html, "title") or "SINGULAR LIVE"
    return re.sub(r"\s*-\s*SINGULAR LIVE\s*$", "", title)


def login_state(html: str) -> Optional[bool]:
    """頁首按鈕：logout-button → 已登入；login-button → 未登入；都沒有（頁面未載入完）→ None"""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("button.logout-button"):
        return True
    if soup.select_one("button.login-button"):
        return False
    return None


def region_blocked(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ").lower()
    return any(n.lower() in text for n in REGION_NOTICES)


# ---------------- 選擇要下載的內容 ----------------

def _time(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()


@dataclass
class Target:
    ticket_id: str
    content_id: str             # 直播間網址用（tickets[n].contents[n].id）
    uliza_id: str
    label: str
    kind: str                   # live / archive
    available: bool             # 現在可以觀看
    opening: Optional[datetime] = None
    ticket_type: str = ""


def plan_targets(
    event: dict, owned: Optional[set[str]], now: datetime,
    ticket_id: Optional[str] = None, content_id: Optional[str] = None, skip: Iterable[str] = (),
) -> tuple[list[Target], list[str]]:
    """回傳 (要下載的內容, 說明訊息)

    - 直播票：直播已結束且有 related_archive → 下載存檔；尚未結束 → 直播（開播前標記為未開放）
    - 存檔票：直接下載
    - owned 為已購買的 ticket id；None 表示無法確認（不過濾）
    """
    skip = set(skip)
    targets: list[Target] = []
    notes: list[str] = []
    tickets = event.get("tickets") or []
    if not tickets:
        notes.append("此活動尚未建立票券 / 直播間")
        return [], notes

    seen: set[str] = set()
    for ticket in tickets:
        tid = ticket["id"]
        ttype = str(ticket.get("type", ""))
        if ticket_id and tid != ticket_id:
            continue
        if owned is not None and tid not in owned:
            notes.append(f"未購買的票券：{ttype}（{ticket.get('price', '?')} 円）")
            continue
        for c in ticket.get("contents") or []:
            cid = c["id"]
            if content_id and cid != content_id:
                continue
            if {tid, cid, c.get("uliza_content_id", "")} & skip:
                continue
            label = c.get("label") or event_title(event)
            if c.get("type") == "archive":
                target = Target(tid, cid, c["uliza_content_id"], label, "archive", True, ticket_type=ttype)
            else:
                live = c.get("live_detail") or {}
                archive = (c.get("related_archive") or {}).get("archive_content")
                if live.get("status") == "ended":
                    if not archive:
                        notes.append(f"「{label}」直播已結束，存檔尚未提供")
                        continue
                    target = Target(tid, cid, archive["uliza_content_id"], archive.get("label") or label,
                                    "archive", True, ticket_type=ttype)
                else:
                    opening = _time(live.get("opening_time") or live.get("start_time"))
                    target = Target(tid, cid, c["uliza_content_id"], label, "live",
                                    opening is None or opening <= now, opening, ttype)
            if target.uliza_id in seen:
                continue        # 直播票與存檔票指向同一內容
            seen.add(target.uliza_id)
            targets.append(target)
    return targets, notes


# ---------------- ULIZA 播放器參數 ----------------

def parse_uliza_params(js: str) -> dict:
    """(function() { var params = {...}; ... }) 中的 params JSON"""
    m = re.search(r"\bparams\s*=\s*", js)
    if not m:
        raise SingularError("ULIZA 回應中找不到 params")
    start = js.index("{", m.end())
    try:
        params, _ = json.JSONDecoder().raw_decode(js, start)
    except json.JSONDecodeError as e:
        raise SingularError(f"ULIZA params 不是合法的 JSON：{e}") from e
    return params


def dig(obj: Any, *paths: str) -> Any:
    """依序嘗試多個以 . 分隔的路徑，回傳第一個存在的值"""
    for path in paths:
        cur = obj
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur not in (None, "", [], {}):
            return cur
    return None


@dataclass
class UlizaInfo:
    video: str
    title: Optional[str]
    poster: Optional[str]
    slides: list[str]
    seek_preview: Optional[str]


def uliza_info(params: dict) -> UlizaInfo:
    video = dig(params, "src.video", "video", "source.video")
    if isinstance(video, dict):
        video = dig(video, "url", "src", "hls")
    if not video:
        raise SingularError("ULIZA params 中沒有影片網址（src.video）")
    settings = dig(params, "src.settings", "settings") or {}
    slides = dig(settings, "posterSlideShow.posters") or []
    slides = [s if isinstance(s, str) else dig(s, "url", "src") for s in slides]
    title = dig(settings, "title.textJa", "title.text", "videoAnalytics.contentTitle", "title")
    return UlizaInfo(
        video=video,
        title=title if isinstance(title, str) else None,      # 實際格式為 {"textJa": "..."}
        poster=dig(params, "src.poster", "poster"),
        slides=[s for s in slides if s],
        seek_preview=dig(settings, "seekpreview.url", "seekPreview.url"),
    )
