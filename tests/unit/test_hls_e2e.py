"""端對端：本機 HTTP 伺服器模擬一個需要 cookie、AES-128（隱含 IV）、可回溯的 HLS 串流"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from Crypto.Cipher import AES

from streamdl.core.models import MediaJob, StreamKind, StreamSpec
from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.protocol.hls.decrypt import resolve_iv
from streamdl.session import cookies as ct

KEY = bytes(range(16))
FIRST_EXISTING, FIRST_LISTED, LAST = 100, 110, 119      # 伺服器有 100~119，清單只列 110~119
SEQ_OFFSET = 10                                         # 清單 MEDIA-SEQUENCE=10 對應檔案 110


def plain(n: int) -> bytes:
    return f"segment-{n:05d}".encode() * 50


def encrypted(n: int) -> bytes:
    data = plain(n)
    pad = 16 - len(data) % 16
    iv = bytes.fromhex(resolve_iv(None, n - FIRST_LISTED + SEQ_OFFSET))
    return AES.new(KEY, AES.MODE_CBC, iv).encrypt(data + bytes([pad]) * pad)


class Handler(BaseHTTPRequestHandler):
    refreshed = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        cookie = self.headers.get("Cookie", "")
        if "token=good" not in cookie:
            self.send_response(403)
            self.end_headers()
            return
        path = self.path.split("?")[0]
        body = None
        if path == "/live/master.m3u8":
            body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=900\nhigh/index.m3u8\n"
        elif path == "/live/high/index.m3u8":
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{SEQ_OFFSET}",
                     '#EXT-X-KEY:METHOD=AES-128,URI="/keys/k1"']
            for n in range(FIRST_LISTED, LAST + 1):
                lines += ["#EXTINF:2.0,", f"seg/part_{n:05d}.ts"]
            lines.append("#EXT-X-ENDLIST")
            body = "\n".join(lines).encode()
        elif path == "/keys/k1":
            body = KEY
        elif path.startswith("/live/high/seg/part_"):
            n = int(path[-8:-3])
            if FIRST_EXISTING <= n <= LAST:
                body = encrypted(n)
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
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.mark.parametrize("fetcher", ["httpx", "curl"])
def test_hls_backfill_decrypt_and_refresh(server, tmp_path, fetcher):
    refreshes = 0

    async def main():
        nonlocal refreshes
        opts = Options(output=tmp_path, fetcher=fetcher, merge=False, decrypt=True, backfill=True, concurrency=4, retries=3, live_idle_limit=1)
        pipe = Pipeline(opts)
        session = pipe.sessions.create(headers={"User-Agent": "test"})
        session.set_cookies([ct.make_cookie("token", "bad", domain="127.0.0.1")])

        async def refresher(s):     # 模擬提取器重新登入
            nonlocal refreshes
            refreshes += 1
            s.set_cookies([ct.make_cookie("token", "good", domain="127.0.0.1")])

        session.set_refresher(refresher)
        job = MediaJob(streams=[StreamSpec(kind=StreamKind.HLS, url=f"{server}/live/master.m3u8",
                                           title="demo", session_id=session.id)])
        try:
            return (await pipe.execute(job)).ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()

    assert asyncio.run(main()) is True
    assert refreshes == 1

    frag = tmp_path / "backup" / "demo" / "fragments"
    dec = tmp_path / "backup" / "demo" / "decrypted"
    for n in range(FIRST_EXISTING, LAST + 1):
        assert (dec / f"{n:010d}.ts").read_bytes() == plain(n), n

    playlist = (frag / "media.m3u8").read_text()
    # 回溯片段的 IV 由估算的媒體序號推出，與伺服器實際加密一致
    assert f"IV=0x{resolve_iv(None, 0)}" in playlist
    assert playlist.count("#EXTINF") == LAST - FIRST_EXISTING + 1
    assert "#EXT-X-KEY" not in (dec / "media.m3u8").read_text()
