import asyncio

import pytest

from src.core.models import MediaJob, StreamKind
from src.core.options import Options
from src.extractor import ExtractContext
from src.extractor.adapters import browser_watch
from src.extractor.adapters.browser_watch import BrowserWatchExtractor
from src.extractor.adapters.zan_live.extractor import ZanLiveExtractor
from src.session import SessionManager
from src.utils import log


class ScriptedBrowser:
    """依序回放 (current_url, [requests]) 的假瀏覽器"""
    def __init__(self, script):
        self.script = list(script)
        self.step = ("", [])
        self.visited = []
        self.closed = False

    def goto(self, url):
        self.visited.append(url)

    @property
    def current_url(self):
        if self.script:
            self.step = self.script.pop(0)
        return self.step[0]

    def drain_media_requests(self):
        reqs, self.step = self.step[1], (self.step[0], [])
        return reqs

    @property
    def user_agent(self):
        return "WatchUA"

    @property
    def cookies(self):
        return [{"name": "sid", "value": "abc", "domain": ".cdn.example", "path": "/"}]

    def execute_async_script(self, script, *args):
        return "我的直播 / 2026"

    def close(self):
        self.closed = True


def make_ctx(fake, **opts):
    ctx = ExtractContext(options=Options(**opts), sessions=SessionManager())

    async def new_browser(headless=None, profile=False):
        ctx.browsers.append(fake)
        return fake

    ctx.new_browser = new_browser
    return ctx


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(browser_watch, "POLL", 0)


def test_detects_stream_and_uses_player_headers(monkeypatch):
    answers = iter([False, True])      # 第一個略過，第二個下載
    monkeypatch.setattr(log, "confirm", lambda *a, **k: asyncio.sleep(0, next(answers)))
    page = "https://site.example/watch/1"
    browser = ScriptedBrowser([
        (page, []),
        (page, [{"url": "https://cdn.example/ad/ad.m3u8", "headers": {}},
                {"url": "https://cdn.example/live/master.m3u8?t=1",
                 "headers": {"Referer": "https://player.example/embed/1", "X-Other": "no"},
                 "document_url": "https://player.example/embed/1"},
                {"url": "https://cdn.example/live/master.m3u8?t=1", "headers": {}}]),   # 重複請求不再詢問
    ])

    async def main():
        ctx = make_ctx(browser)
        job = await BrowserWatchExtractor().extract("", ctx)
        session = ctx.sessions.get(job.streams[0].session_id)
        result = (job, session.headers_for("https://cdn.example/live/seg1.ts"), session._sync_task is not None)
        await ctx.sessions.aclose()
        return result

    job, headers, syncing = asyncio.run(main())
    assert browser.visited == [browser_watch.START_PAGE]
    assert len(job.streams) == 1 and job.streams[0].kind is StreamKind.HLS
    assert job.streams[0].url == "https://cdn.example/live/master.m3u8?t=1"
    assert job.streams[0].title == "我的直播 ⧸ 2026"
    assert headers["Referer"] == "https://player.example/embed/1"         # iframe 播放器的 Referer
    assert headers["Origin"] == "https://player.example"
    assert headers["User-Agent"] == "WatchUA" and headers["Cookie"] == "sid=abc"
    assert "X-Other" not in headers
    assert syncing                                                          # 下載期間持續從瀏覽器同步


def test_hands_off_to_site_extractor(monkeypatch):
    called = {}

    async def fake_extract(self, url, ctx):
        called["url"] = url
        return MediaJob(title="zan")

    monkeypatch.setattr(ZanLiveExtractor, "extract", fake_extract)
    zan_url = "https://www.zan-live.com/zh-TW/live/detail/10782"
    browser = ScriptedBrowser([("https://www.google.com/", []), (zan_url, [])])
    ctx = make_ctx(browser)
    job = asyncio.run(BrowserWatchExtractor().extract("", ctx))
    assert job.title == "zan" and called["url"] == zan_url
    assert browser.closed and browser not in ctx.browsers    # 交接後關閉監控瀏覽器，不留空白視窗


def test_stop_event_ends_watch():
    browser = ScriptedBrowser([])

    async def main():
        ctx = make_ctx(browser)
        asyncio.get_running_loop().call_later(0.05, ctx.stop.set)
        return await BrowserWatchExtractor().extract("https://site.example/", ctx)

    job = asyncio.run(main())
    assert job.streams == [] and browser.visited == ["https://site.example/"]


def test_refuses_without_browser():
    with pytest.raises(browser_watch.BrowserWatchError):
        asyncio.run(BrowserWatchExtractor().extract("", make_ctx(ScriptedBrowser([]), browser="never")))
