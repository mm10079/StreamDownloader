"""HLS 主播放清單以 EXT-X-MEDIA 指定獨立音訊軌：影像與音訊分別下載後合併"""
import asyncio
import functools
import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from streamdl.core.models import MediaJob, StreamKind, StreamSpec
from streamdl.core.options import Options
from streamdl.core.pipeline import Pipeline
from streamdl.protocol.hls import parser

MASTER = """#EXTM3U
#EXT-X-MEDIA:LANGUAGE="jpn",AUTOSELECT=YES,CHANNELS="2",TYPE=AUDIO,URI="chunklist__audio1.m3u8",GROUP-ID="audio_0",DEFAULT=NO,NAME="jpn"
#EXT-X-MEDIA:LANGUAGE="eng",AUTOSELECT=YES,CHANNELS="2",TYPE=AUDIO,URI="chunklist__audio0.m3u8",GROUP-ID="audio_0",DEFAULT=YES,NAME="eng"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=160x120,AUDIO="audio_0"
chunklist_video.m3u8
"""


def test_audio_for_prefers_default_then_autoselect():
    master = parser.parse_master(MASTER, "https://h/live/playlist.m3u8")
    audio = master.audio_for(master.variants[0])
    assert audio.name == "eng" and audio.uri == "https://h/live/chunklist__audio0.m3u8"

    no_default = MASTER.replace("DEFAULT=YES", "DEFAULT=NO")
    master = parser.parse_master(no_default, "https://h/live/playlist.m3u8")
    assert master.audio_for(master.variants[0]).name == "jpn"

    # 沒有 URI 的 EXT-X-MEDIA：音訊已包含在畫質片段中
    muxed = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="x",DEFAULT=YES\n' \
            '#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="a"\nv.m3u8\n'
    master = parser.parse_master(muxed, "https://h/m.m3u8")
    assert master.audio_for(master.variants[0]) is None


ffmpeg = shutil.which("ffmpeg")
ffprobe = shutil.which("ffprobe")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    common = ["-y", "-hide_banner", "-loglevel", "error"]
    hls = ["-f", "hls", "-hls_time", "1", "-hls_list_size", "0", "-hls_playlist_type", "vod"]
    subprocess.run([ffmpeg, *common, "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3",
                    "-c:v", "libx264", "-g", "10", *hls, "-hls_segment_filename", str(root / "v_%03d.ts"),
                    str(root / "chunklist_video.m3u8")], check=True)
    for name, freq in (("audio0", 440), ("audio1", 880)):
        subprocess.run([ffmpeg, *common, "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3",
                        "-c:a", "aac", *hls, "-hls_segment_filename", str(root / f"{name}_%03d.ts"),
                        str(root / f"chunklist__{name}.m3u8")], check=True)
    (root / "playlist.m3u8").write_text(MASTER)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.mark.skipif(not (ffmpeg and ffprobe), reason="需要 ffmpeg / ffprobe")
def test_separate_audio_track_downloaded_and_merged(server, tmp_path):
    out = tmp_path / "out"

    async def main():
        pipe = Pipeline(Options(output=out, merge=True, backfill=False, concurrency=4, retries=1))
        session = pipe.sessions.create()
        job = MediaJob(streams=[StreamSpec(kind=StreamKind.HLS, url=f"{server}/playlist.m3u8",
                                           title="demo", session_id=session.id)])
        try:
            return await pipe.execute(job)
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()

    result = asyncio.run(main())
    assert result.ok

    backup = out / "backup" / "demo"
    assert (backup / "playlists" / "playlist.m3u8").exists()          # 主播放清單在最上層
    for track in ("video", "audio"):                                     # 影像、音訊各自獨立的資料夾
        assert list((backup / track / "fragments").glob("*.ts")), track
        assert (backup / track / "fragments" / "media.m3u8").exists()
        assert (backup / track / "store.json").exists()
    assert not (backup / "fragments").exists()

    final = out / "demo.mp4"
    probe = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(final)],
        capture_output=True, check=True).stdout)
    assert sorted(s["codec_type"] for s in probe["streams"]) == ["audio", "video"]


# ---------------- SAMPLE-AES fMP4：影像與音訊為不同 KID ----------------

KID_V, KEY_V = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90"), bytes.fromhex("11" * 16)
KID_A, KEY_A = bytes.fromhex("5e6f708192a3b4c5d6e7f8091a2b3c4d"), bytes.fromhex("22" * 16)

DRM_MASTER = """#EXTM3U
#EXT-X-MEDIA:LANGUAGE="eng",AUTOSELECT=YES,TYPE=AUDIO,URI="audio.m3u8",GROUP-ID="audio_0",DEFAULT=NO,NAME="eng"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=160x120,AUDIO="audio_0"
video.m3u8
"""


def _split_fmp4(path):
    from .cenc_fixture import parse
    boxes = parse(path.read_bytes())
    init = b"".join(b.serialize() for b in boxes if b.type in (b"ftyp", b"moov"))
    frags, cur = [], b""
    for b in boxes:
        if b.type == b"moof" and cur:
            frags.append(cur)
            cur = b""
        if b.type not in (b"ftyp", b"moov"):
            cur += b.serialize()
    return init, frags + [cur]


@pytest.fixture
def drm_server(tmp_path):
    from .cenc_fixture import Scheme, encrypt_init, encrypt_segment
    root = tmp_path / "srv"
    root.mkdir()
    common = ["-y", "-hide_banner", "-loglevel", "error"]
    frag = ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]
    subprocess.run([ffmpeg, *common, "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
                    "-c:v", "libx264", "-g", "10", *frag, str(tmp_path / "v.mp4")], check=True)
    subprocess.run([ffmpeg, *common, "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:a", "aac", *frag, "-frag_duration", "1000000", str(tmp_path / "a.mp4")], check=True)
    for name, kid, key in (("video", KID_V, KEY_V), ("audio", KID_A, KEY_A)):
        scheme = Scheme("cenc", key=key, kid=kid)
        init, frags = _split_fmp4(tmp_path / f"{name[0]}.mp4")
        (root / f"{name}_init.mp4").write_bytes(encrypt_init(init, scheme))
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0",
                 f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://drm?keyId={kid.hex()}",KEYFORMAT="com.apple.streamingkeydelivery"',
                 f'#EXT-X-MAP:URI="{name}_init.mp4"']
        for i, f in enumerate(frags):
            (root / f"{name}_{i}.mp4").write_bytes(encrypt_segment(f, scheme))
            lines += ["#EXTINF:1.0,", f"{name}_{i}.mp4"]
        (root / f"{name}.m3u8").write_text("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n")
    (root / "playlist.m3u8").write_text(DRM_MASTER)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.mark.skipif(not (ffmpeg and ffprobe), reason="需要 ffmpeg / ffprobe")
@pytest.mark.parametrize("key", [
    f"{KID_A.hex()}:{KEY_A.hex()},{KID_V.hex()}:{KEY_V.hex()}",     # 依 KID 對應（音訊金鑰列在前面）
    f"{KEY_A.hex()},{KEY_V.hex()}",                                  # 沒有 KID：以 ffmpeg 試解選出
], ids=["by-kid", "trial"])
def test_sample_aes_tracks_with_different_kids(drm_server, tmp_path, key):
    out = tmp_path / "out"

    async def main():
        pipe = Pipeline(Options(output=out, merge=True, backfill=False, retries=1, key=key))
        session = pipe.sessions.create()
        job = MediaJob(streams=[StreamSpec(kind=StreamKind.HLS, url=f"{drm_server}/playlist.m3u8",
                                           title="drm", session_id=session.id)])
        try:
            return await pipe.execute(job)
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()

    assert asyncio.run(main()).ok
    backup = out / "backup" / "drm"
    assert next((backup / "video" / "fragments").glob("key_*.key")).read_bytes() == KEY_V
    assert next((backup / "audio" / "fragments").glob("key_*.key")).read_bytes() == KEY_A

    final = out / "drm.mp4"
    for stream in ("v", "a"):        # 兩軌都能正常解碼（金鑰錯誤只會得到雜訊）
        res = subprocess.run([ffmpeg, "-v", "error", "-i", str(final), "-map", f"0:{stream}", "-f", "null", "-"],
                             capture_output=True, text=True)
        assert res.returncode == 0 and not res.stderr.strip(), (stream, res.stderr[:300])


# ---------------- 兩軌起點不同：依 PROGRAM-DATE-TIME 或共同序號對齊 ----------------

def _hls_lines(path):
    """ffmpeg 產生的 VOD 清單 → [(EXTINF 秒數, 片段檔名)]"""
    lines = path.read_text().splitlines()
    return [(float(l.split(":")[1].rstrip(",")), lines[i + 1]) for i, l in enumerate(lines) if l.startswith("#EXTINF")]


@pytest.fixture
def offset_server(tmp_path, request):
    """同一個來源切成影像、音訊兩條 HLS（時間軸相同），其中一軌少了開頭 2 段"""
    late, with_pdt = request.param
    root = tmp_path / "srv"
    root.mkdir()
    common = ["-y", "-hide_banner", "-loglevel", "error"]
    src = tmp_path / "src.ts"
    subprocess.run([ffmpeg, *common, "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
                    "-c:v", "libx264", "-g", "10", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    hls = ["-f", "hls", "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "vod"]
    for name, stream in (("video", "0:v"), ("audio", "0:a")):
        subprocess.run([ffmpeg, *common, "-copyts", "-i", str(src), "-map", stream, "-c", "copy", *hls,
                        "-hls_segment_filename", str(root / f"{name}_%02d.ts"), str(tmp_path / f"{name}.m3u8")], check=True)
    expected = 0.0
    for name in ("video", "audio"):
        entries = _hls_lines(tmp_path / f"{name}.m3u8")
        skip = 2 if name == late else 0
        if skip:
            expected = sum(d for d, _ in entries[:skip]) * (1 if name == "audio" else -1)
        t = 1_791_581_992.0 + sum(d for d, _ in entries[:skip])      # 兩軌共用的時鐘
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:3", f"#EXT-X-MEDIA-SEQUENCE:{skip}"]
        for d, f in entries[skip:]:
            if with_pdt:
                stamp = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
            lines += [f"#EXTINF:{d:.6f},", f]
            t += d
        (root / f"{name}.m3u8").write_text("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n")
    (root / "playlist.m3u8").write_text(DRM_MASTER)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", expected, with_pdt
    httpd.shutdown()


@pytest.mark.skipif(not (ffmpeg and ffprobe), reason="需要 ffmpeg / ffprobe")
@pytest.mark.parametrize("offset_server", [("audio", True), ("audio", False), ("video", True)],
                         ids=["audio-late-pdt", "audio-late-sequence", "video-late-pdt"], indirect=True)
def test_tracks_with_different_start_are_aligned(offset_server, tmp_path):
    url, expected, with_pdt = offset_server
    out = tmp_path / "out"

    async def main():
        pipe = Pipeline(Options(output=out, merge=True, backfill=False, retries=1))
        session = pipe.sessions.create()
        job = MediaJob(streams=[StreamSpec(kind=StreamKind.HLS, url=f"{url}/playlist.m3u8",
                                           title="sync", session_id=session.id)])
        try:
            return await pipe.execute(job)
        finally:
            await pipe.fetcher.aclose()
            await pipe.sessions.aclose()

    assert asyncio.run(main()).ok
    probe = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json", str(out / "sync.mp4")],
        capture_output=True, check=True).stdout)
    start = {s["codec_type"]: float(s["start_time"]) for s in probe["streams"]}
    assert abs(expected) > 3                                         # 確實有起點差
    assert abs((start["audio"] - start["video"]) - expected) < 0.3, (start, expected)
    local = (out / "backup" / "sync" / "video" / "fragments" / "media.m3u8").read_text()
    assert ("#EXT-X-PROGRAM-DATE-TIME:" in local) == with_pdt                    # 本地清單保留時間標記
