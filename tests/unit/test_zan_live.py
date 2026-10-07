import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from src.core.options import Options
from src.core.pipeline import Pipeline
from src.extractor.adapters.zan_live import pages
from src.extractor.adapters.zan_live.extractor import ZanLiveExtractor
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


# ---------------- 純函式 ----------------

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


# ---------------- 模擬 ZAN-LIVE 伺服器 ----------------

class ZanMock(BaseHTTPRequestHandler):
    base = ""
    logins = 0

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    @property
    def logged_in(self):
        return "nglives_pltk=ok" in self.headers.get("Cookie", "")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        if self.path.startswith("/zh-TW/auth/login") and form.get("_csrf") == ["tok123"] \
                and form.get("password") == ["pw"]:
            type(self).logins += 1
            return self._send(302, headers=[("Location", "/zh-TW/"),
                                            ("Set-Cookie", "nglives_pltk=ok; Path=/; HttpOnly")])
        return self._send(200, '<input name="_csrf" value="tok123">')

    def do_GET(self):
        path = urlsplit(self.path).path
        b = self.base
        if path == "/zh-TW/auth/login":
            return self._send(200, '<form><input type="hidden" name="_csrf" value="tok123"></form>')
        if path == "/zh-TW/":
            return self._send(200, "home")
        if path == "/api/live/detail/tickets":
            past = NOW - timedelta(hours=1)
            body = {"result": [ticket(5752, 3619, past, True), ticket(5753, 3620, past, True)]}
            return self._send(200, json.dumps(body), "application/json")
        if path == "/zh-TW/live/detail/777":
            return self._send(200, f"""<meta property="og:title" content="測試公演 / Day1">
                <meta property="og:image" content="{b}/img/cover.jpg">
                <img class="topTitle" src="{b}/img/top.jpg">
                <script>var ticketGroupArtists = [{{"id":"a1","name":"Artist","detail":"","imageUrl":"/img/artist.jpg",
                "displayOrderDesc":1,"links":[]}}];</script>""")
        if path.startswith("/zh-TW/live/play/"):
            if not self.logged_in:
                return self._send(302, headers=[("Location", "/zh-TW/auth/login")])
            tid = path.rstrip("/").split("/")[-2]
            return self._send(200, f"""<meta name="ticket-group-id" content="777">
                <meta name="live-name" content="視角 {tid}"><meta name="live-url" content="{b}/hls/{tid}/index.m3u8">
                <meta name="normalGifts" content='{json.dumps([gift("g1")])}'>
                <meta name="vod-comment-manifest-url" content="{b}/comments/manifest.json">""")
        if path == "/comments/manifest.json":
            return self._send(200, json.dumps({"comments": {f"{b}/comments/1.json": 1}}), "application/json")
        if path.startswith("/hls/"):
            # 串流需要登入 cookie 與正確 Referer（舊版 headers 不一致的問題）
            tid = path.split("/")[2]
            if not self.logged_in or f"/live/play/{tid}/" not in self.headers.get("Referer", ""):
                return self._send(403)
            if path.endswith("index.m3u8"):
                pl = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n"
                pl += "".join(f"#EXTINF:2.0,\nseg{n}.ts\n" for n in range(3)) + "#EXT-X-ENDLIST\n"
                return self._send(200, pl, "application/vnd.apple.mpegurl")
            return self._send(200, f"{tid}-{path}".encode() * 10, "video/mp2t")
        if path.startswith("/img/") or path.startswith("/comments/"):
            return self._send(200, b"data", "application/octet-stream")
        return self._send(404)


@pytest.fixture
def zan_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ZanMock)
    ZanMock.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ZanMock.logins = 0
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ZanMock.base
    httpd.shutdown()


def test_zan_end_to_end(zan_server, tmp_path):
    async def main():
        opts = Options(output=tmp_path, account="me@example.com", password="pw", merge=False, browser="never")
        pipe = Pipeline(opts)
        extractor = ZanLiveExtractor(urls=ZanUrls(domain=zan_server))
        try:
            job = await extractor.extract("https://www.zan-live.com/zh-TW/live/detail/777", pipe.ctx)
            ok = await pipe.execute(job)
            return job, ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()

    job, ok = asyncio.run(main())
    assert ok
    assert ZanMock.logins == 2                              # 每個視角各自登入
    assert [s.title for s in job.streams] == ["視角 5752", "視角 5753"]
    assert len({s.session_id for s in job.streams}) == 2    # 獨立 session
    root = tmp_path / "測試公演 ⧸ Day1"
    for tid in ("5752", "5753"):
        frags = sorted((root / "backup" / f"視角 {tid}" / "fragments").glob("*.ts"))
        assert len(frags) == 3
    room = root / "視角 5752"
    assert (room / "cover.jpg").exists() and (room / "top.jpg").exists()
    assert (room / "images" / "artists" / "artist.jpg").exists()
    assert (room / "images" / "gifts" / "gift_g1.png").exists()
    assert (room / "raw comments" / "視角 5752" / "1.json").exists()
    assert json.loads((room / "web info" / "tickets" / "liveTickets.json").read_text(encoding="utf-8"))[0]["id"] == "5752"
    assert (room / "web info" / "gifts" / "normalGifts.json").exists()
