import asyncio
import html as htmllib
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.extractor.adapters.singular_live import extractor as sl_extractor
from streamdl.extractor.adapters.singular_live import pages
from streamdl.extractor.adapters.singular_live.extractor import SingularLiveExtractor
from streamdl.extractor.adapters.singular_live.urls import SingularUrls, parse_detail, parse_play

from .test_zan_live_e2e import BROWSER_UA, FakeBrowser

DATA = Path(__file__).parent.parent / "data" / "singular_live"
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def sample(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def detail_html(event: dict, title="活動 - SINGULAR LIVE", image="/static/events/x/kv") -> str:
    attr = htmllib.escape(json.dumps(event, ensure_ascii=False), quote=True)
    return (f'<html><head><meta property="og:title" content="{title}"><meta property="og:image" content="{image}">'
            f'</head><body><div id="react-root" data-lang="zh" data-event="{attr}"></div></body></html>')


# ---------------- 解析 ----------------

def test_urls():
    assert parse_detail("https://singular-live.thinkr.jp/zh/event/detail/01a0998e-70b2-73b3#ticket-info") \
        == ("zh", "01a0998e-70b2-73b3")
    assert parse_play("https://singular-live.thinkr.jp/ja/play/01a0f131-86a5/01a09990-1ff2") \
        == ("ja", "01a0f131-86a5", "01a09990-1ff2")
    u = SingularUrls(lang="ja")
    assert u.play("t", "c") == "https://singular-live.thinkr.jp/ja/play/t/c"
    assert "name=abc&playerobjectname=player" in u.uliza_params("abc")


def test_parse_event_from_escaped_attribute():
    event = sample("live_ended_with_archive")
    html = detail_html(event)
    assert pages.parse_event(html) == event
    assert pages.event_title(event, html) == "SINGULAR LIVE TV（仮題）vol.１"
    assert pages.event_title({}, html) == "活動"          # 沒有本地化標題時用 og:title 並去掉網站名
    assert pages.parse_event("<html></html>") is None


def test_region_notice():
    assert pages.region_blocked("<div>本活动仅限日本国内播放，日本境外无法观看，敬请谅解。</div>")
    assert not pages.region_blocked("<div>正常的直播間</div>")


def test_plan_live_ended_uses_related_archive():
    targets, notes = pages.plan_targets(sample("live_ended_with_archive"), {"01a0f131-86a5-70a7-b5dc-bcb7d4939ad5"}, NOW)
    assert len(targets) == 1 and not notes
    t = targets[0]
    assert (t.kind, t.uliza_id, t.available) == ("archive", "SINGULAR_LIVE_TV_01_edit", True)
    assert (t.ticket_id, t.content_id) == ("01a0f131-86a5-70a7-b5dc-bcb7d4939ad5", "01a09990-1ff2-730b-8b4e-301224535049")


def test_plan_archive_purchased_and_not_purchased():
    targets, _ = pages.plan_targets(sample("archive_purchased"), {"01a032a7-fd17-70d2-8b5f-971e4e7eef1a"}, NOW)
    assert [t.uliza_id for t in targets] == ["content-2026-08-25-17-46-40-109-82010889"]
    targets, notes = pages.plan_targets(sample("archive_not_on_sale"), set(), NOW)
    assert targets == [] and "未購買" in notes[0] and "3300" in notes[0]


def test_plan_no_tickets():
    targets, notes = pages.plan_targets(sample("live_no_tickets"), set(), NOW)
    assert targets == [] and "尚未建立" in notes[0]


def test_plan_upcoming_live_and_ended_without_archive():
    event = sample("live_ended_with_archive")
    content = event["tickets"][0]["contents"][0]
    content["live_detail"].update(status="scheduled", opening_time="2026-10-09T10:55:00.000000Z")
    content["related_archive"] = None
    targets, _ = pages.plan_targets(event, None, NOW)
    assert targets[0].kind == "live" and not targets[0].available
    assert targets[0].uliza_id == "content-2026-09-13-15-59-14-315-6c2eea1b"
    content["live_detail"]["status"] = "ended"
    targets, notes = pages.plan_targets(event, None, NOW)
    assert targets == [] and "存檔尚未提供" in notes[0]


def test_uliza_params_real_structure():
    js = (DATA / "uliza_params.js").read_text(encoding="utf-8").replace("{VIDEO}", "https://v/index.m3u8?sig=1") \
        .replace("{CDN}", "https://cdn")
    info = pages.uliza_info(pages.parse_uliza_params(js))
    assert info.video == "https://v/index.m3u8?sig=1"
    assert info.title == "SINGULAR LIVE TV（仮題）vol.1 のアーカイブ"
    assert info.poster.startswith("https://cdn/") and len(info.slides) == 10
    assert info.seek_preview.startswith("https://cdn/")


# ---------------- 端對端 ----------------

EVENT_ID = "01a0998e-70b2-73b3-a614-d5ad6d6e11bf"
TICKET = "01a0f131-86a5-70a7-b5dc-bcb7d4939ad5"
CONTENT = "01a09990-1ff2-730b-8b4e-301224535049"


class SingularMock(BaseHTTPRequestHandler):
    base = ""
    vpn = False                 # 使用者是否已開啟日本 VPN
    owned: list = []
    play_visitors: list = []
    uliza_requests: list = []

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=()):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if urlsplit(self.path).path == "/zh/login" and form.get("password") == ["pw"]:
            return self._send(302, headers=[("Location", "/zh/"), ("Set-Cookie", "sl_session=ok; Path=/")])
        return self._send(200, "<p>帳號或密碼錯誤</p>")

    def do_GET(self):
        url = urlsplit(self.path)
        path, b = url.path, self.base
        logged_in = "sl_session=ok" in self.headers.get("Cookie", "")
        detail = f"/zh/event/detail/{EVENT_ID}"
        if path == detail:
            notice = "<p>本活动仅限日本国内播放，日本境外无法观看，敬请谅解。</p>" if "blocked" in url.query else ""
            return self._send(200, detail_html(sample("live_ended_with_archive")).replace("<body>", "<body>" + notice))
        if path == "/zh/login":
            return self._send(200, '<input id="login-email"><input id="login-password">'
                                   '<button class="login-email-submit">登录</button>')
        if path == "/zh/":
            button = "logout-button" if logged_in else "login-button"
            return self._send(200, f'<button class="{button}"></button>')
        if path == "/zh/api/mypage/available-tickets":
            if not logged_in:
                return self._send(401, '{"message":"Unauthenticated."}', "application/json")
            return self._send(200, json.dumps({"tickets": [{"id": t} for t in self.owned]}), "application/json")
        if path.startswith("/zh/play/"):
            type(self).play_visitors.append(self.headers.get("User-Agent"))
            if not self.vpn:
                return self._send(302, headers=[("Location", detail + "?blocked=1")])
            return self._send(200, "<div id='player'></div>")
        if path == "/uliza":
            name = parse_qs(url.query)["name"][0]
            type(self).uliza_requests.append(name)
            js = (DATA / "uliza_params.js").read_text(encoding="utf-8")
            js = js.replace("{VIDEO}", f"{b}/hls/{name}/index.m3u8").replace("{CDN}", f"{b}/cdn")
            return self._send(200, js, "application/javascript")
        if path.startswith("/hls/"):
            if path.endswith("index.m3u8"):
                pl = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n"
                pl += "".join(f"#EXTINF:2.0,\nseg{n}.ts\n" for n in range(3)) + "#EXT-X-ENDLIST\n"
                return self._send(200, pl, "application/vnd.apple.mpegurl")
            return self._send(200, path.encode() * 10, "video/mp2t")
        if path.startswith("/cdn/") or path.startswith("/static/"):
            return self._send(200, b"image", "image/jpeg")
        return self._send(404)


class UserBrowser(FakeBrowser):
    """模擬使用者：過一會兒在瀏覽器登入；被區域限制導回後開 VPN 並手動進入直播間"""
    manual_login = True

    def __init__(self):
        super().__init__()
        self.cookie_reads = 0
        self.last_play = None

    def goto(self, url):
        if "/play/" in url:
            self.last_play = url
        super().goto(url)

    @property
    def cookies(self):
        self.cookie_reads += 1
        if self.manual_login and self.cookie_reads == 3:      # 第 3 次讀取時「使用者完成登入」
            self.client.cookies.set("sl_session", "ok", domain="127.0.0.1", path="/")
        return FakeBrowser.cookies.fget(self)

    @cookies.setter
    def cookies(self, items):
        FakeBrowser.cookies.fset(self, items)

    @property
    def current_url(self):
        if "blocked" in self.url and self.last_play and not SingularMock.vpn:
            SingularMock.vpn = True             # 使用者開啟 VPN
            super().goto(self.last_play)        # 並手動進入直播間
        return self.url


@pytest.fixture
def singular(monkeypatch):
    monkeypatch.setattr(sl_extractor, "ROOM_LOAD", 0)
    monkeypatch.setattr(sl_extractor, "REMIND_EVERY", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SingularMock)
    SingularMock.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    SingularMock.vpn, SingularMock.owned = False, [TICKET]
    SingularMock.play_visitors, SingularMock.uliza_requests = [], []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SingularMock.base
    httpd.shutdown()


class AutoLoginBrowser(UserBrowser):
    """不手動登入；模擬 React 登入表單送出"""
    manual_login = False
    scripts: list = []
    ignored_clicks = 0          # 前幾次點擊沒有反應
    pending_waits = 0           # 前幾次檢查按鈕仍停用（模擬 Turnstile 人機驗證進行中）

    def execute_async_script(self, script, *args):
        type(self).scripts.append(script)
        if "login-email" in script and self.url.endswith("/zh/login"):
            email, password = args
            cls = type(self)
            if cls.pending_waits > 0:
                cls.pending_waits -= 1
                return {"state": "waiting", "turnstile": True}
            if cls.ignored_clicks > 0:
                cls.ignored_clicks -= 1
                return {"state": "clicked", "turnstile": True}
            r = self.client.post(self.url, data={"email": email, "password": password})
            self.page, self.url = r.text, str(r.url)
            return {"state": "clicked", "turnstile": True}
        return {"state": "no-form", "turnstile": False}


def run_singular(base, tmp_path, browser_cls=None, **opts):
    async def main():
        pipe = Pipeline(Options(output=tmp_path, merge=False, **opts))
        browser = (browser_cls or UserBrowser)()

        async def new_browser(headless=None, profile=False):
            pipe.ctx.browsers.append(browser)
            return browser

        pipe.ctx.new_browser = new_browser
        urls = SingularUrls(domain=base, uliza=base + "/uliza?name={name}&playerobjectname=player")
        try:
            job = await SingularLiveExtractor(urls).extract(f"https://singular-live.thinkr.jp/zh/event/detail/{EVENT_ID}",
                                                            pipe.ctx)
            return job, (await pipe.execute(job)).ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()
            await pipe.ctx.close()
    return asyncio.run(main())


def test_end_to_end_login_region_lock_and_download(singular, tmp_path):
    job, ok = run_singular(singular, tmp_path)
    assert ok
    assert SingularMock.vpn                                     # 經過區域限制 → 手動進入
    assert set(SingularMock.play_visitors) == {BROWSER_UA}       # 直播間只由瀏覽器進入
    assert SingularMock.uliza_requests == ["SINGULAR_LIVE_TV_01_edit"]   # 已結束的直播 → 存檔
    assert [s.title for s in job.streams] == ["SINGULAR LIVE TV（仮題） アーカイブ"]

    root = tmp_path / "SINGULAR LIVE TV（仮題）vol.１"
    assert len(list((root / "backup" / "SINGULAR LIVE TV（仮題） アーカイブ" / "fragments").glob("*.ts"))) == 3
    content = root / "SINGULAR LIVE TV（仮題） アーカイブ"
    event = json.loads((content / "web info" / "attachments" / "event.json").read_text(encoding="utf-8"))
    assert event["id"] == EVENT_ID
    params = json.loads((content / "web info" / "tickets" / "uliza_params.json").read_text(encoding="utf-8"))
    assert params["src"]["name"] == "SINGULAR_LIVE_TV_01_edit"
    images = content / "images"
    assert (images / "cover.jpg").exists() and (images / "poster.jpg").exists()
    assert len(list((images / "slides").iterdir())) == 10
    assert len(list(images.glob("seekpreview_*.jpg"))) == 1
    assert {p.name for p in content.iterdir()} == {"images", "web info"}      # 附件不散落在內容資料夾根目錄


def test_not_purchased_downloads_nothing(singular, tmp_path):
    SingularMock.owned = []
    job, ok = run_singular(singular, tmp_path)
    assert job.streams == [] and SingularMock.uliza_requests == []    # 未購買不會讀取播放參數
    root = tmp_path / "SINGULAR LIVE TV（仮題）vol.１"
    assert (root / "web info" / "attachments" / "event.json").exists()     # 活動資訊仍會存檔
    assert (root / "images" / "cover.jpg").exists()


def test_auto_login_with_credentials(singular, tmp_path):
    AutoLoginBrowser.scripts, AutoLoginBrowser.pending_waits, AutoLoginBrowser.ignored_clicks = [], 0, 0
    job, ok = run_singular(singular, tmp_path, AutoLoginBrowser, account="me@example.com", password="pw")
    assert ok and len(job.streams) == 1
    assert any("login-email" in s and "HTMLInputElement" in s for s in AutoLoginBrowser.scripts)


def test_auto_login_retries_when_click_is_ignored(singular, tmp_path, monkeypatch):
    monkeypatch.setattr(sl_extractor, "LOGIN_CLICK_WAIT", 2)
    AutoLoginBrowser.scripts, AutoLoginBrowser.pending_waits, AutoLoginBrowser.ignored_clicks = [], 0, 1
    job, ok = run_singular(singular, tmp_path, AutoLoginBrowser, account="me@example.com", password="pw")
    assert ok and len(job.streams) == 1
    assert sum("login-email" in s for s in AutoLoginBrowser.scripts) == 2    # 第一次沒反應 → 重試成功


def test_auto_login_waits_for_turnstile_before_clicking(singular, tmp_path, monkeypatch):
    monkeypatch.setattr(sl_extractor, "TURNSTILE_HINT_AFTER", 0)
    AutoLoginBrowser.scripts, AutoLoginBrowser.pending_waits, AutoLoginBrowser.ignored_clicks = [], 3, 0
    job, ok = run_singular(singular, tmp_path, AutoLoginBrowser, account="me@example.com", password="pw")
    assert ok and len(job.streams) == 1
    assert sum("login-email" in s for s in AutoLoginBrowser.scripts) == 4    # 3 次等待驗證 → 第 4 次點擊


def test_auto_login_wrong_password_falls_back_to_manual(singular, tmp_path, monkeypatch):
    monkeypatch.setattr(sl_extractor, "LOGIN_CLICK_WAIT", 1)
    monkeypatch.setattr(sl_extractor, "LOGIN_TIMEOUT", 3)
    AutoLoginBrowser.pending_waits, AutoLoginBrowser.ignored_clicks = 0, 0
    with pytest.raises(pages.SingularError, match="等待登入逾時"):
        run_singular(singular, tmp_path, AutoLoginBrowser, account="me@example.com", password="wrong")


def test_login_state_buttons():
    assert pages.login_state('<button class="logout-button"><span class="--label">LOGOUT</span></button>') is True
    assert pages.login_state('<button class="login-button"><span class="--label">LOGIN</span></button>') is False
    assert pages.login_state("<div>loading</div>") is None
