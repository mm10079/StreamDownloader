import asyncio
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.extractor import find_extractor
from streamdl.extractor.adapters.twitter_space import TwitterSpaceExtractor, folder_name

REAL_URL = ("https://prod-fastly-ap-northeast-1.video.pscp.tv/Transcoding/v1/hls/abc/non_transcode/ap-northeast-1/"
            "periscope-replay-direct-prod-ap-northeast-1-public/audio-space/master_playlist.m3u8")
START = "2026-10-07T14:30:22.596Z"


def test_routing():
    assert find_extractor(REAL_URL).config.name == "twitter-space"
    assert find_extractor("https://example.com/live/index.m3u8").config.name == "direct"


def test_folder_name_uses_broadcast_date():
    broadcast = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)
    local = broadcast.astimezone().strftime("%Y%m%d")
    assert folder_name("歌枠", broadcast) == f"{local} - Twitter Space #歌枠"
    assert folder_name("#已有井號", broadcast) == f"{local} - Twitter Space #已有井號"
    assert folder_name("a/b", broadcast).endswith("#a⧸b")


class SpaceMock(BaseHTTPRequestHandler):
    live = False
    referers: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).referers.append(self.headers.get("Referer"))
        path = urlsplit(self.path).path
        if path.endswith("master_playlist.m3u8"):
            # 與真實格式相同：variant 為絕對路徑，CODECS 標示 avc1 但內容是 AAC
            body = ("#EXTM3U\n#EXT-X-DYNAMICALLY-GENERATED\n"
                    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=500000,CODECS="avc1.640015,mp4a.40.2"\n'
                    "/Transcoding/v1/hls/abc/transcode/token/audio-space/playlist_1665.m3u8\n")
        elif path.endswith("playlist_1665.m3u8"):
            body = "#EXTM3U\n" + ("" if self.live else "#EXT-X-PLAYLIST-TYPE:VOD\n") + \
                   "#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
            for n in range(4):
                body += f"#EXT-X-PROGRAM-DATE-TIME:{START}\n#EXTINF:3.0,\nchunk_17913834226{n:05d}_{n}_a.aac\n"
            body += "" if self.live else "#EXT-X-ENDLIST\n"
        elif path.endswith(".aac"):
            if "/transcode/" in path:
                # 與真實伺服器相同：以媒體播放清單路徑解析的片段網址回傳 400，片段實際位於主播放清單路徑下
                self.send_response(400)
                self.end_headers()
                return
            body = path.encode() * 20
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def space_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SpaceMock)
    SpaceMock.live, SpaceMock.referers = False, []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/Transcoding/v1/hls/abc/non_transcode/audio-space/master_playlist.m3u8"
    httpd.shutdown()


def run(url, tmp_path, **opts):
    async def main():
        pipe = Pipeline(Options(output=tmp_path, merge=False, **opts))
        try:
            job = await TwitterSpaceExtractor().extract(url, pipe.ctx)
            return job, (await pipe.execute(job)).ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()
    return asyncio.run(main())


def test_space_download(space_server, tmp_path):
    job, ok = run(space_server, tmp_path, title="歌枠リレー")
    assert ok
    expected = folder_name("歌枠リレー", datetime.fromisoformat(START.replace("Z", "+00:00")))
    assert job.output_dir == tmp_path / expected
    stream = job.streams[0]
    assert stream.title == "歌枠リレー" and stream.backfill is False
    frags = sorted((tmp_path / expected / "backup" / "歌枠リレー" / "fragments").glob("*.aac"))
    assert len(frags) == 4
    assert set(SpaceMock.referers) == {"https://x.com/"}            # 所有請求都帶 x.com Referer


def test_default_title_without_prompt(space_server, tmp_path):
    job, ok = run(space_server, tmp_path)                        # 非互動環境：不詢問，使用預設標題
    assert ok and job.title == "Twitter Space"
    assert job.output_dir.name.endswith("Twitter Space #Twitter Space")


def test_custom_referer_option(space_server, tmp_path):
    run(space_server, tmp_path, title="t", referer="https://twitter.com/")
    assert set(SpaceMock.referers) == {"https://twitter.com/"}
