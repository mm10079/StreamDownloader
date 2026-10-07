import json
from datetime import datetime, timedelta, timezone

from src.extractor.adapters.zan_live import pages
from src.extractor.adapters.zan_live.schema.item import LiveRoomMetas
from src.extractor.adapters.zan_live.urls import ZanUrls, parse_detail, parse_playroom

NOW = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def ticket(tid, live_id, open_at, is_live=False, **kw):
    return {
        "id": str(tid), "name": f"視角{tid}", "liveName": f"Live {tid}", "liveId": live_id, "originLiveId": live_id,
        "isLive": is_live, "openLiveDate": iso(open_at), "liveBeginDate": iso(open_at),
        "liveEndDate": iso(open_at + timedelta(hours=3)), "isArchiveStream": False,
        "haveViewableTicket": True, "isShowStreamBeginDate": True, **kw,
    }


def gift(gid):
    return {
        "id": gid, "liveId": None, "name": "g", "consumePoint": 1, "iconUrl": "", "commentIconUrl": "",
        "listIconUrl": f"/img/gift_{gid}.png", "isCharged": False, "backgroundColor": "", "backgroundImage": "",
        "backgroundImageRepeat": False, "nameFontColor": "", "giftFontColor": "", "detailFontColor": "",
        "detailBackColor": "", "borderColor": "", "comIconPosRight": 0, "comIconPosTop": 0, "comIconScale": 1.0,
        "priority": 0, "isValid": True, "group": 0, "displayTime": 0, "usageText": "", "createdAt": "", "updatedAt": "",
    }


def test_url_parsing():
    assert parse_detail("https://www.zan-live.com/zh-TW/live/detail/10656#play-pages") == "10656"
    assert parse_detail("https://www.zan-live.com/live/detail/1") == "1"
    assert parse_playroom("https://www.zan-live.com/ja/live/play/5752/3619") == ("5752", "3619")
    assert parse_playroom("https://www.zan-live.com/zh-TW/live/detail/1") is None
    assert ZanUrls().absolute("//cdn.x/a.png") == "https://cdn.x/a.png"
    assert ZanUrls().absolute("/img/a.png") == "https://www.zan-live.com/img/a.png"


def test_select_live_tickets_are_all_angles():
    raw = [ticket(1, 11, NOW - timedelta(hours=1), True), ticket(2, 12, NOW - timedelta(hours=1), True),
           ticket(3, 13, NOW + timedelta(days=1))]
    assert [t.id for t in pages.select_tickets(raw, NOW)] == ["1", "2"]


def test_select_earliest_upcoming_and_skip_expired():
    raw = [ticket(1, 11, NOW - timedelta(days=1)),                      # 過期
           ticket(2, 12, NOW + timedelta(days=2)),
           ticket(3, 13, NOW + timedelta(days=1)), ticket(4, 14, NOW + timedelta(days=1))]
    assert [t.id for t in pages.select_tickets(raw, NOW)] == ["3", "4"]
    assert [t.id for t in pages.select_tickets(raw, NOW, skip={"3"})] == ["4"]


def test_select_specific_ticket():
    raw = [ticket(1, 11, NOW - timedelta(hours=1), True), ticket(2, 12, NOW - timedelta(hours=1), True)]
    assert [t.id for t in pages.select_tickets(raw, NOW, ticket_id="2", live_id="12")] == ["2"]


def test_import_metas_parses_json_arrays():
    html = f"""<meta name="live-name" content="A &amp; B"><meta name="live-url" content="https://x/a.m3u8">
    <meta name="normalGifts" content='{json.dumps([gift("g1")])}'>"""
    metas = pages.import_metas(LiveRoomMetas, pages.soup_of(html))
    assert metas.Livename == "A & B" and metas.Liveurl == "https://x/a.m3u8"
    assert list(pages.gifts_of(metas)) == ["normalGifts"]


def test_import_metas_tolerates_empty_and_bracketed_text():
    # 真實頁面：部分 meta content 為空字串；標題可能以括號開頭但不是 JSON
    html = """<meta name="live-name" content="[DAY1] 公演"><meta name="live-url" content="https://x/a.m3u8">
    <meta name="specialGifts" content=""><meta name="live-banners" content="">
    <meta name="seek-bar-thumbnail-vtt-url" content="">"""
    metas = pages.import_metas(LiveRoomMetas, pages.soup_of(html))
    assert metas.Livename == "[DAY1] 公演"
    assert metas.SpecialGifts == [] and metas.LiveBanners == [] and metas.SeekBarthumbnailVttUrl is None
