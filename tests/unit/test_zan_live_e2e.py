"""模擬 ZAN-LIVE 伺服器的端對端測試

伺服器規則（對應真實網站的限制）：
- 每次登入取得不同 token
- 進入直播間時，該 token 成為此視角的「擁有者」，並取得只在直播間發放的 cf_{tid} cookie
- 串流只允許「最後進入此直播間的 token」+ cf cookie + 瀏覽器 UA + 正確 Referer 下載
"""
import asyncio
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.core.options import Options
from src.core.pipeline import Pipeline
from src.extractor.adapters.zan_live.extractor import ZanLiveExtractor
from src.extractor.adapters.zan_live.urls import ZanUrls
from src.session import cookies as ct

from .test_zan_live import NOW, gift, ticket

BROWSER_UA = "FakeBrowser/1.0"


class ZanMock(BaseHTTPRequestHandler):
    base = ""
    logins = 0
    room_owner: dict = {}
    room_visitors_ua: list = []

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

    def cookies(self) -> dict:
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    @property
    def token(self):
        t = self.cookies().get("nglives_pltk", "")
        return t if t.startswith("ok") else None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        if self.path.startswith("/zh-TW/auth/login") and form.get("_csrf") == ["tok123"] \
                and form.get("password") == ["pw"]:
            type(self).logins += 1
            return self._send(302, headers=[("Location", "/zh-TW/"),
                                            ("Set-Cookie", f"nglives_pltk=ok{self.logins}; Path=/; HttpOnly")])
        return self._send(200, '<input name="_csrf" value="tok123">')

    def do_GET(self):
        path = urlsplit(self.path).path
        b = self.base
        if path == "/zh-TW/auth/login":
            return self._send(200, '<form><input type="hidden" name="_csrf" value="tok123"></form>')
        if path in ("/zh-TW/", "/", ""):
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
            type(self).room_visitors_ua.append(self.headers.get("User-Agent"))
            if not self.token:
                return self._send(302, headers=[("Location", "/zh-TW/auth/login")])
            tid = path.rstrip("/").split("/")[-2]
            type(self).room_owner[tid] = self.token
            return self._send(200, f"""<meta name="ticket-group-id" content="777">
                <meta name="live-name" content="視角 {tid}"><meta name="live-url" content="{b}/hls/{tid}/index.m3u8">
                <meta name="normalGifts" content='{json.dumps([gift("g1")])}'>
                <meta name="specialGifts" content="">
                <meta name="vod-comment-manifest-url" content="{b}/comments/manifest.json">""",
                              headers=[("Set-Cookie", f"cf_{tid}=signed; Path=/")])
        if path == "/comments/manifest.json":
            return self._send(200, json.dumps({"comments": {f"{b}/comments/1.json": 1}}), "application/json")
        if path.startswith("/hls/"):
            tid = path.split("/")[2]
            if (self.token is None or self.room_owner.get(tid) != self.token
                    or self.cookies().get(f"cf_{tid}") != "signed"
                    or f"/live/play/{tid}/" not in self.headers.get("Referer", "")
                    or self.headers.get("User-Agent") != BROWSER_UA):
                return self._send(403)
            if path.endswith("index.m3u8"):
                pl = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n"
                pl += "".join(f"#EXTINF:2.0,\nseg{n}.ts\n" for n in range(3)) + "#EXT-X-ENDLIST\n"
                return self._send(200, pl, "application/vnd.apple.mpegurl")
            return self._send(200, f"{tid}-{path}".encode() * 10, "video/mp2t")
        if path.startswith("/img/") or path.startswith("/comments/"):
            return self._send(200, b"data", "application/octet-stream")
        return self._send(404)


class FakeBrowser:
    """以同步 httpx 模擬瀏覽器：自有 cookie jar、自有 UA"""
    def __init__(self):
        self.client = httpx.Client(follow_redirects=True, headers={"User-Agent": BROWSER_UA})
        self.page = ""
        self.url = ""
        self.closed = False

    def goto(self, url):
        r = self.client.get(url)
        self.page, self.url = r.text, str(r.url)

    @property
    def content(self):
        return self.page

    @property
    def current_url(self):
        return self.url

    @property
    def user_agent(self):
        return BROWSER_UA

    @property
    def cookies(self):
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure}
                for c in self.client.cookies.jar]

    @cookies.setter
    def cookies(self, items):
        self.client.cookies.jar.clear()
        for c in ct.from_browser(items):
            self.client.cookies.jar.set_cookie(c)

    def close(self):
        self.closed = True
        self.client.close()


@pytest.fixture
def zan_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ZanMock)
    ZanMock.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ZanMock.logins = 0
    ZanMock.room_owner = {}
    ZanMock.room_visitors_ua = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ZanMock.base
    httpd.shutdown()


def run_zan(server, tmp_path, url, before_execute=None, **opt_kwargs):
    browsers: list[FakeBrowser] = []

    async def main():
        opts = Options(output=tmp_path, account="me@example.com", password="pw", merge=False, **opt_kwargs)
        pipe = Pipeline(opts)

        async def new_browser(headless=None):
            b = FakeBrowser()
            browsers.append(b)
            pipe.ctx.browsers.append(b)
            return b

        pipe.ctx.new_browser = new_browser
        try:
            job = await ZanLiveExtractor(urls=ZanUrls(domain=server)).extract(url, pipe.ctx)
            if before_execute:
                before_execute()
            return job, await pipe.execute(job)
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()
            await pipe.ctx.close()

    job, ok = asyncio.run(main())
    return job, ok, browsers


def test_each_angle_has_own_login_and_browser(zan_server, tmp_path):
    job, ok, browsers = run_zan(zan_server, tmp_path, "https://www.zan-live.com/zh-TW/live/detail/777")
    assert ok
    assert ZanMock.logins == 2                              # 每個視角各自登入
    assert len(browsers) == 2 and all(b.closed for b in browsers)
    assert set(ZanMock.room_visitors_ua) == {BROWSER_UA}    # 直播間只由瀏覽器進入，httpx 從未碰觸
    assert ZanMock.room_owner == {"5752": "ok1", "5753": "ok2"}
    assert [s.title for s in job.streams] == ["視角 5752", "視角 5753"]
    assert len({s.session_id for s in job.streams}) == 2

    root = tmp_path / "測試公演 ⧸ Day1"
    for tid in ("5752", "5753"):
        assert len(list((root / "backup" / f"視角 {tid}" / "fragments").glob("*.ts"))) == 3
    room = root / "視角 5752"
    assert (room / "cover.jpg").exists() and (room / "top.jpg").exists()
    assert (room / "images" / "artists" / "artist.jpg").exists()
    assert (room / "images" / "gifts" / "gift_g1.png").exists()
    assert (room / "raw comments" / "視角 5752" / "1.json").exists()
    tickets = json.loads((room / "web info" / "tickets" / "liveTickets.json").read_text(encoding="utf-8"))
    assert tickets[0]["id"] == "5752"


def test_kicked_session_recovers_by_reentering_room(zan_server, tmp_path):
    """下載前直播間被其他 session 進入（例如手動重新整理）→ 403
    → 先從瀏覽器同步 cookies（無效）→ 短時間內再次 403 → 瀏覽器重新進入直播間後恢復"""
    def kick():
        ZanMock.room_owner["5752"] = "someone-else"

    job, ok, browsers = run_zan(zan_server, tmp_path, "https://www.zan-live.com/zh-TW/live/play/5752/3619",
                                before_execute=kick, retries=4)
    assert ok
    assert len(job.streams) == 1 and len(browsers) == 1     # 直播間網址只下載該視角
    assert ZanMock.room_owner["5752"].startswith("ok")
    assert set(ZanMock.room_visitors_ua) == {BROWSER_UA}
