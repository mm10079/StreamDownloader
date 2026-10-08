"""加密 / DRM 判斷：可下載（金鑰公開或使用者持有）與不可下載（授權解密模組）的界線"""
import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from streamdl.core.models import MediaJob, StreamKind, StreamSpec
from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.protocol.dash.parser import parse_mpd
from streamdl.protocol.hls import parser as hls

from .cenc_fixture import Scheme, encrypt_init, encrypt_segment, parse, synthetic_fmp4

# ---------------- HLS ----------------

def media(key_lines: str) -> str:
    return f"#EXTM3U\n#EXT-X-TARGETDURATION:2\n{key_lines}\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n"


def test_hls_identity_key_is_downloadable():
    for method in ("AES-128", "SAMPLE-AES"):
        seg = hls.parse_media(media(f'#EXT-X-KEY:METHOD={method},URI="k.bin"'), "https://h/v/i.m3u8").segments[0]
        assert seg.key.method == method and seg.key.uri == "https://h/v/k.bin" and not seg.key.drm


@pytest.mark.parametrize("line, name", [
    ('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc",KEYFORMAT="com.apple.streamingkeydelivery"', "FairPlay"),
    ('#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;base64,AAA",'
     'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"', "Widevine"),
    ('#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:x",KEYFORMAT="com.microsoft.playready"', "PlayReady"),
])
def test_hls_drm_detected(line, name):
    key = hls.parse_media(media(line), "https://h/i.m3u8").segments[0].key
    assert key.drm and key.drm_name == name


def test_hls_prefers_identity_key_among_multiple():
    lines = ('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x",KEYFORMAT="com.apple.streamingkeydelivery"\n'
             '#EXT-X-KEY:METHOD=AES-128,URI="https://k/1"')
    key = hls.parse_media(media(lines), "https://h/i.m3u8").segments[0].key
    assert key.method == "AES-128" and not key.drm


def test_hls_key_group_resets_after_segment():
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k1"\n#EXTINF:2,\na.ts\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="k2"\n#EXTINF:2,\nb.ts\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\nc.ts\n')
    segs = hls.parse_media(text, "https://h/i.m3u8").segments
    assert [s.key.uri.rsplit("/", 1)[-1] if s.key else None for s in segs] == ["k1", "k2", None]


# ---------------- DASH 解析 ----------------

KEY = bytes.fromhex("100b6c20940f779a4589152b57d2dacb")
KID = bytes.fromhex("eb676abbcb345e96bbcf616630f1a3da")
KID_UUID = "eb676abb-cb34-5e96-bbcf-616630f1a3da"
WIDEVINE = '<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>'
CLEARKEY = ('<ContentProtection schemeIdUri="urn:uuid:e2719d58-a985-b3c9-781a-b030af78d30e">'
            '<clearkey:Laurl xmlns:clearkey="http://dashif.org/guidelines/clearKey">/license</clearkey:Laurl>'
            '</ContentProtection>')


def dash_mpd(protection: str) -> str:
    cenc = (f'<ContentProtection xmlns:cenc="urn:mpeg:cenc:2013" schemeIdUri="urn:mpeg:dash:mp4protection:2011" '
            f'value="cenc" cenc:default_KID="{KID_UUID}"/>' if protection else "")
    return f'''<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period><AdaptationSet contentType="video" mimeType="video/mp4">{cenc}{protection}
        <SegmentTemplate timescale="1" duration="2" media="v-$Number$.m4s" initialization="v-init.mp4"/>
        <Representation id="v" bandwidth="1"/></AdaptationSet></Period></MPD>'''


def test_dash_content_protection_parsed():
    rep = parse_mpd(dash_mpd(WIDEVINE + CLEARKEY), "https://h/live/x.mpd").representations[0]
    assert rep.protected and rep.default_kid == KID.hex()
    assert rep.drm_systems == ["Widevine", "ClearKey"]
    assert rep.clearkey_laurl == "https://h/license"
    assert not parse_mpd(dash_mpd(""), "https://h/x.mpd").representations[0].protected


def test_key_option_parsing():
    assert Options(key=f"{KID_UUID}:{KEY.hex().upper()}").key_map == {KID.hex(): KEY.hex()}
    assert Options(key=KEY.hex()).key_map == {"": KEY.hex()}


# ---------------- DASH 端對端 ----------------

class DrmMock(BaseHTTPRequestHandler):
    mpd = ""
    files: dict[str, bytes] = {}
    license_ok = False
    license_calls = 0

    def log_message(self, *args):
        pass

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path.lstrip("/")
        body = self.mpd.encode() if path == "x.mpd" else self.files.get(path)
        self._send(200, body) if body is not None else self._send(404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).license_calls += 1
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        if not self.license_ok or req.get("kids") != [b64(KID)]:
            return self._send(403)
        self._send(200, json.dumps({"keys": [{"kty": "oct", "kid": b64(KID), "k": b64(KEY)}]}).encode())


@pytest.fixture
def drm_server():
    import os
    samples = [[os.urandom(n) for n in (400, 33)], [os.urandom(n) for n in (250, 90, 17)]]
    init, frags = synthetic_fmp4(samples)
    scheme = Scheme("cenc", key=KEY, kid=KID)
    DrmMock.files = {"v-init.mp4": encrypt_init(init, scheme),
                     "v-1.m4s": encrypt_segment(frags[0], scheme), "v-2.m4s": encrypt_segment(frags[1], scheme)}
    DrmMock.license_ok, DrmMock.license_calls = False, 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DrmMock)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", [b"".join(f) for f in samples]
    httpd.shutdown()


def run_dash(url, tmp_path, **opts):
    async def main():
        pipe = Pipeline(Options(output=tmp_path, merge=False, backfill=False, **opts))
        sid = pipe.sessions.create().id
        try:
            return (await pipe.execute(MediaJob(streams=[StreamSpec(kind=StreamKind.DASH, url=url, title="d", session_id=sid)]))).ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()
    return asyncio.run(main())


def decrypted_payloads(tmp_path):
    data = (tmp_path / "backup" / "d" / "video" / "video.decrypted.mp4").read_bytes()
    return [b.data for b in parse(data) if b.type == b"mdat"]


def test_dash_decrypts_with_user_key(drm_server, tmp_path):
    url, clear = drm_server
    DrmMock.mpd = dash_mpd(WIDEVINE)                         # 即使標示 Widevine，持有金鑰即可解密
    assert run_dash(f"{url}/x.mpd", tmp_path, key=f"{KID.hex()}:{KEY.hex()}") is True
    assert decrypted_payloads(tmp_path) == clear


def test_dash_clearkey_license(drm_server, tmp_path):
    url, clear = drm_server
    DrmMock.mpd = dash_mpd(CLEARKEY)
    DrmMock.license_ok = True
    assert run_dash(f"{url}/x.mpd", tmp_path) is True
    assert DrmMock.license_calls == 1 and decrypted_payloads(tmp_path) == clear


def test_dash_drm_without_key_is_refused_before_download(drm_server, tmp_path):
    url, _ = drm_server
    DrmMock.mpd = dash_mpd(WIDEVINE)
    assert run_dash(f"{url}/x.mpd", tmp_path) is False
    assert not (tmp_path / "backup" / "d" / "video").exists()      # 確認金鑰前不會白下載


def test_dash_wrong_key_fails(drm_server, tmp_path):
    url, clear = drm_server
    DrmMock.mpd = dash_mpd(WIDEVINE)
    assert run_dash(f"{url}/x.mpd", tmp_path, key=f"{KID.hex()}:{'00' * 16}") is True   # 下載與「解密」都會完成
    assert decrypted_payloads(tmp_path) != clear                    # 但內容錯誤（CTR 無法偵測金鑰錯誤）
