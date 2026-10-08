import asyncio
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from streamdl.core.models import MediaJob, StreamKind, StreamSpec
from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.protocol.dash.parser import MpdError, fill_template, parse_duration, parse_mpd, to_url_template

NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011"'


def mpd(body: str, **attrs) -> str:
    a = " ".join(f'{k}="{v}"' for k, v in {"type": "static", **attrs}.items())
    return f'<?xml version="1.0"?><MPD {NS} {a}>{body}</MPD>'


# ---------------- 解析 ----------------

def test_duration_and_template_fill():
    assert parse_duration("PT1H2M3.5S") == 3723.5
    assert parse_duration("P1DT1S") == 86401
    assert fill_template("v_$RepresentationID$_$Number%05d$_$Bandwidth$.m4s", "720p", 3000, number=7) \
        == "v_720p_00007_3000.m4s"
    assert fill_template("a_$Time$_$$.m4s", time=123) == "a_123_$.m4s"


def test_inherited_template_baseurl_chain_and_timeline_repeat():
    text = mpd("""<BaseURL>cdn/</BaseURL><Period>
      <AdaptationSet mimeType="video/mp4"><BaseURL>v/</BaseURL>
        <SegmentTemplate timescale="1000" media="$RepresentationID$/$Number$.m4s" initialization="$RepresentationID$/init.mp4" startNumber="10">
          <SegmentTimeline><S t="0" d="2000" r="-1"/><S t="8000" d="1000"/></SegmentTimeline>
        </SegmentTemplate>
        <Representation id="hi" bandwidth="5000" height="1080"/>
        <Representation id="lo" bandwidth="1000" height="360"><SegmentTemplate startNumber="20"/></Representation>
      </AdaptationSet></Period>""", mediaPresentationDuration="PT9S")
    m = parse_mpd(text, "https://h/live/x.mpd")
    hi, lo = m.by_type("video")
    assert hi.id == "hi" and hi.init_url == "https://h/live/cdn/v/hi/init.mp4"
    # r=-1 重複到下一個 S 的起點（8000）→ 4 段，加上最後一段
    assert [s.key for s in hi.segments] == [10, 11, 12, 13, 14]
    assert hi.segments[0].url == "https://h/live/cdn/v/hi/10.m4s" and hi.segments[-1].duration == 1.0
    assert lo.segments[0].key == 20                       # Representation 覆寫 startNumber
    assert hi.url_template.format(3) == "https://h/live/cdn/v/hi/3.m4s"


def test_static_duration_template_count():
    text = mpd("""<Period><AdaptationSet contentType="audio" lang="ja">
        <SegmentTemplate timescale="10" duration="40" media="a-$Number$.aac"/>
        <Representation id="a" bandwidth="128000"/></AdaptationSet></Period>""",
               mediaPresentationDuration="PT10S")
    rep = parse_mpd(text, "https://h/a.mpd").by_type("audio")[0]
    assert [s.key for s in rep.segments] == [1, 2, 3]     # 10 秒 / 4 秒 → 3 段
    assert rep.lang == "ja"


def test_dynamic_duration_template_uses_now_and_window():
    ast = datetime(2026, 1, 1, tzinfo=timezone.utc)
    text = mpd("""<Period start="PT0S"><AdaptationSet mimeType="video/mp4">
        <SegmentTemplate timescale="1" duration="2" startNumber="1" media="s$Number$.ts"/>
        <Representation id="v" bandwidth="1"/></AdaptationSet></Period>""",
               type="dynamic", availabilityStartTime="2026-01-01T00:00:00Z", timeShiftBufferDepth="PT10S",
               minimumUpdatePeriod="PT2S")
    m = parse_mpd(text, "https://h/l.mpd", now=ast + timedelta(seconds=100))
    keys = [s.key for s in m.representations[0].segments]
    # 100 秒 / 2 秒 = 50 段已完成，扣掉邊緣緩衝 2 段 → 最新 48；視窗 10 秒 = 5 段
    assert keys == [44, 45, 46, 47, 48]
    assert m.dynamic and m.minimum_update_period == 2


def test_time_template_keys_and_durations():
    text = mpd("""<Period><AdaptationSet mimeType="audio/mp4">
        <SegmentTemplate timescale="44100" media="c-$Time$.m4s" presentationTimeOffset="0">
          <SegmentTimeline><S t="0" d="84992"/><S d="88064" r="1"/></SegmentTimeline></SegmentTemplate>
        <Representation id="1" bandwidth="1"/></AdaptationSet></Period>""", mediaPresentationDuration="PT6S")
    rep = parse_mpd(text, "https://h/a.mpd").representations[0]
    assert rep.key_kind == "time"
    assert [s.key for s in rep.segments] == [0, 84992, 173056]
    assert rep.durations == [84992, 88064, 88064]
    assert rep.url_template.format(5) == "https://h/c-5.m4s"


def test_segment_list_and_drm_and_single_file():
    text = mpd("""<Period>
      <AdaptationSet mimeType="video/mp4"><ContentProtection schemeIdUri="urn:uuid:edef8ba9"/>
        <Representation id="drm" bandwidth="1"><BaseURL>v.mp4</BaseURL>
          <SegmentList timescale="1" duration="2"><Initialization range="0-99"/>
            <SegmentURL mediaRange="100-199"/><SegmentURL mediaRange="200-299"/></SegmentList>
        </Representation></AdaptationSet>
      <AdaptationSet mimeType="audio/mp4"><Representation id="a" bandwidth="1"><BaseURL>a.m4a</BaseURL>
        <SegmentBase indexRange="0-10"/></Representation></AdaptationSet></Period>""",
               mediaPresentationDuration="PT4S")
    m = parse_mpd(text, "https://h/x.mpd")
    v, a = m.by_type("video")[0], m.by_type("audio")[0]
    assert v.protected and v.init_url == "https://h/v.mp4" and v.init_range == (0, 99)
    assert [(s.key, s.byte_range) for s in v.segments] == [(1, (100, 199)), (2, (200, 299))]
    assert a.key_kind == "index" and a.segments[0].url == "https://h/a.m4a" and not a.protected


def test_url_template_rejects_mixed_variables_and_escapes_braces():
    assert to_url_template("$Number$_$Time$.m4s", "https://h/", "r", 1) is None
    t = to_url_template("{x}/$RepresentationID$-$Number%03d$.m4s", "https://h/", "v1", 1)
    assert t.format(7) == "https://h/{x}/v1-007.m4s" and t.fill == 3


def test_invalid_mpd():
    with pytest.raises(MpdError):
        parse_mpd("<html/>", "https://h/x.mpd")


# ---------------- 端對端 ----------------

def timeline_mpd(start: int, end: int, dynamic: bool, drm: bool = False) -> str:
    """影像 / 音訊兩軌，$Number$ 為 start..end"""
    def aset(kind, rep_id):
        prot = '<ContentProtection schemeIdUri="urn:uuid:x"/>' if drm else ""
        return f"""<AdaptationSet contentType="{kind}" mimeType="{kind}/mp4">{prot}
          <SegmentTemplate timescale="1" media="{kind}-$Number$.m4s" initialization="{kind}-init.mp4" startNumber="{start}">
            <SegmentTimeline><S t="{(start - 1) * 2}" d="2" r="{end - start}"/></SegmentTimeline></SegmentTemplate>
          <Representation id="{rep_id}" bandwidth="1000"/></AdaptationSet>"""
    attrs = {"type": "dynamic", "minimumUpdatePeriod": "PT1S", "availabilityStartTime": "2026-01-01T00:00:00Z"} \
        if dynamic else {"mediaPresentationDuration": f"PT{end * 2}S"}
    return mpd(f"<Period>{aset('video', 'v')}{aset('audio', 'a')}</Period>", **attrs)


class DashMock(BaseHTTPRequestHandler):
    manifests: list[str] = []
    served = 0
    available = range(1, 7)     # 伺服器實際存在的片段序號

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/live.mpd":
            cls = type(self)
            body = cls.manifests[min(cls.served, len(cls.manifests) - 1)].encode()
            cls.served += 1
        elif path.endswith("-init.mp4"):
            body = f"INIT:{path}|".encode()
        else:
            kind, num = path.strip("/").rsplit(".", 1)[0].split("-")
            body = f"{kind}{num}|".encode() if int(num) in self.available else None
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        status = 200
        if rng:
            a, b = rng.split("=")[1].split("-")
            body, status = body[int(a): int(b) + 1], 206
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def dash_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashMock)
    DashMock.served = 0
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def run_job(url, tmp_path, **opts):
    async def main():
        pipe = Pipeline(Options(output=tmp_path, merge=False, live_idle_limit=3, **opts))
        session = pipe.sessions.create()
        job = MediaJob(streams=[StreamSpec(kind=StreamKind.DASH, url=url, title="dash", session_id=session.id)])
        try:
            return (await pipe.execute(job)).ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()
    return asyncio.run(main())


def read_track(tmp_path, kind):
    return (tmp_path / "backup" / "dash" / kind / f"{kind}.mp4").read_text()


def test_live_mpd_updates_until_static_with_backfill(dash_server, tmp_path):
    # 直播視窗 3~4 → 3~5 → 轉為 static 3~6；回溯應補回 1~2
    DashMock.manifests = [timeline_mpd(3, 4, True), timeline_mpd(3, 5, True), timeline_mpd(3, 6, False)]
    assert run_job(f"{dash_server}/live.mpd", tmp_path) is True
    assert DashMock.served >= 3
    assert read_track(tmp_path, "video") == "INIT:/video-init.mp4|" + "".join(f"video{n}|" for n in range(1, 7))
    assert read_track(tmp_path, "audio").endswith("audio5|audio6|")


def test_backfill_disabled(dash_server, tmp_path):
    DashMock.manifests = [timeline_mpd(3, 6, False)]
    assert run_job(f"{dash_server}/live.mpd", tmp_path, backfill=False) is True
    assert read_track(tmp_path, "video") == "INIT:/video-init.mp4|video3|video4|video5|video6|"


def test_drm_is_rejected(dash_server, tmp_path):
    DashMock.manifests = [timeline_mpd(1, 6, False, drm=True)]
    assert run_job(f"{dash_server}/live.mpd", tmp_path) is False
    assert not (tmp_path / "backup" / "dash" / "video").exists()
