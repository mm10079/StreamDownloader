"""--key 提供多組金鑰時，以實際片段試解選出正確的一組"""
import asyncio
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from Crypto.Cipher import AES

from streamdl.core.models import MediaJob, StreamKind, StreamSpec
from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.postprocess.cenc import pick_cenc_key
from streamdl.protocol.hls.decrypt import check_aes128, resolve_iv

KEY = bytes(range(16))
WRONG = [bytes([0xAA] * 16), bytes([0x55] * 16)]
SEGMENTS = 4


def ts_like(n: int) -> bytes:
    """MPEG-TS 外觀的內容：每 188 bytes 以 0x47 開頭"""
    return b"".join(b"\x47" + bytes([n, i % 256]) + bytes(185) for i in range(20))


def encrypt(data: bytes, key: bytes, iv_hex: str) -> bytes:
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).encrypt(data + bytes([pad]) * pad)


def test_check_aes128():
    iv = resolve_iv(None, 7)
    data = encrypt(ts_like(1), KEY, iv)
    assert check_aes128(data, KEY, iv) is True
    assert all(check_aes128(data, k, iv) is False for k in WRONG)
    # 格式不明但填充正確 → None（多半正確）
    assert check_aes128(encrypt(b"x" * 100, KEY, iv), KEY, iv) is None


def test_key_list_keeps_all_keys():
    kid = "0123456789abcdef0123456789abcdef"
    opts = Options(key=f"{WRONG[0].hex()}, {kid}:{KEY.hex()},{WRONG[0].hex()}")
    assert opts.key_list == [("", WRONG[0].hex()), (kid, KEY.hex())]


class HlsMock(BaseHTTPRequestHandler):
    segment_hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/index.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0",
                     '#EXT-X-KEY:METHOD=AES-128,URI="skd://not-downloadable"']
            for n in range(SEGMENTS):
                lines += ["#EXTINF:2.0,", f"s{n}.ts"]
            body = "\n".join(lines + ["#EXT-X-ENDLIST"]).encode()
        elif path.startswith("/s"):
            type(self).segment_hits += 1
            n = int(path[2:-3])
            body = encrypt(ts_like(n), KEY, resolve_iv(None, n))
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def hls_server():
    HlsMock.segment_hits = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HlsMock)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def run_hls(url, tmp_path, key):
    async def main():
        pipe = Pipeline(Options(output=tmp_path, merge=False, decrypt=True, backfill=False, retries=1, key=key))
        sid = pipe.sessions.create().id
        try:
            return (await pipe.execute(MediaJob(streams=[StreamSpec(
                kind=StreamKind.HLS, url=f"{url}/index.m3u8", title="h", session_id=sid)]))).ok
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()
    return asyncio.run(main())


def test_hls_picks_correct_key_among_many(hls_server, tmp_path):
    keys = ",".join(k.hex() for k in [WRONG[0], KEY, WRONG[1]])
    assert run_hls(hls_server, tmp_path, keys) is True
    dec = tmp_path / "backup" / "h" / "decrypted"
    for n in range(SEGMENTS):
        assert (dec / f"{n:010d}.ts").read_bytes() == ts_like(n)
    assert HlsMock.segment_hits == SEGMENTS          # 試解用的片段不會重複下載


def test_hls_single_key_is_used_without_check(hls_server, tmp_path):
    """只有一組 --key 時不試解，照常下載並解密（即使金鑰不對）"""
    assert run_hls(hls_server, tmp_path, WRONG[0].hex()) is True
    assert len(list((tmp_path / "backup" / "h" / "decrypted").glob("*.ts"))) == SEGMENTS


def test_hls_no_key_matches_falls_back_to_first(hls_server, tmp_path):
    """多組都試不出來時用第一組，照常下載並解密，不中止"""
    assert run_hls(hls_server, tmp_path, ",".join(k.hex() for k in WRONG)) is True
    assert len(list((tmp_path / "backup" / "h" / "decrypted").glob("*.ts"))) == SEGMENTS
    assert HlsMock.segment_hits == SEGMENTS


ffmpeg = shutil.which("ffmpeg")


@pytest.mark.skipif(not ffmpeg, reason="需要 ffmpeg")
def test_pick_cenc_key(tmp_path):
    """以 ffmpeg 產生可解碼的 fMP4，再以 cenc_fixture 加密（與 Shaka Packager 等相同，含 senc）"""
    from .cenc_fixture import Scheme, encrypt_init, encrypt_segment, parse

    clear = tmp_path / "clear.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2", "-c:v", "libx264",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(clear)], check=True)
    boxes = parse(clear.read_bytes())
    init = b"".join(b.serialize() for b in boxes if b.type in (b"ftyp", b"moov"))
    frags = [b.serialize() for b in boxes if b.type not in (b"ftyp", b"moov")]
    scheme = Scheme("cenc", key=KEY, kid=bytes.fromhex("0123456789abcdef0123456789abcdef"))
    sample = tmp_path / "enc.mp4"
    sample.write_bytes(encrypt_init(init, scheme) + encrypt_segment(b"".join(frags), scheme))
    wrong = [k.hex() for k in WRONG]

    assert asyncio.run(pick_cenc_key(sample, [wrong[0], KEY.hex(), wrong[1]])) == KEY.hex()
    assert asyncio.run(pick_cenc_key(sample, wrong)) is None
    assert not (tmp_path / "enc.trial.mp4").exists()
