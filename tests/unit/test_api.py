import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pydantic
import pytest

import streamdl
from streamdl.core.models import StreamKind
from streamdl.utils import log


class HlsMock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.endswith("master.m3u8"):
            body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nmedia.m3u8\n"
        elif path.endswith("media.m3u8"):
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            body += "".join(f"#EXTINF:2.0,\nseg{n}.ts\n" for n in range(3)) + "#EXT-X-ENDLIST\n"
        elif path.endswith(".ts") and path.startswith("/ok/"):
            body = f"{path}|" * 20
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HlsMock)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_public_names():
    assert streamdl.__version__
    assert {"download", "adownload", "DownloadResult", "Options"} <= set(streamdl.__all__)


def test_download_returns_structured_result(server, tmp_path):
    result = streamdl.download(f"{server}/ok/master.m3u8", output=tmp_path, title="範例", merge=False, retries=1)
    assert result.ok and result.error is None
    assert result.extractor == "direct" and result.title == "範例"
    stream = result.streams[0]
    assert (stream.kind, stream.ok, stream.failed, stream.output) == (StreamKind.HLS, True, [], None)   # 未合併
    assert stream.backup == tmp_path / "backup" / "範例"
    assert len(list((stream.backup / "fragments").glob("*.ts"))) == 3


def test_failed_segments_reported(server, tmp_path):
    result = streamdl.download(f"{server}/broken/master.m3u8", output=tmp_path, title="壞", merge=False, retries=1)
    assert not result.ok
    assert len(result.streams[0].failed) == 3 and all(u.endswith(".ts") for u in result.streams[0].failed)


def test_unsupported_url_sets_error(tmp_path):
    result = streamdl.download("ftp://example.com/x", output=tmp_path)
    assert not result.ok and "沒有支援此網址的提取器" in result.error and result.streams == []


def test_progress_hook_events(server, tmp_path):
    events = []
    streamdl.download(f"{server}/ok/master.m3u8", output=tmp_path, title="進度", merge=False,
                      progress_hook=events.append)
    logs = [e for e in events if e["type"] == "log"]
    progress = [e for e in events if e["type"] == "progress"]
    assert any("使用提取器" in e["message"] for e in logs)
    assert all("[" not in e["message"] or "進度" in e["message"] for e in logs)     # 已去除 Rich 標記
    statuses = [e["status"] for e in progress]
    assert statuses[0] == "started" and statuses[-1] == "finished" and "running" in statuses
    last_running = [e for e in progress if e["status"] == "running"][-1]
    assert last_running["completed"] == 3 and last_running["total"] == 3
    assert "進度" in progress[0]["description"]
    assert len({e["id"] for e in progress}) == 1


def test_hook_errors_do_not_break_download(server, tmp_path):
    def bad_hook(event):
        raise ValueError("hook 壞了")
    assert streamdl.download(f"{server}/ok/master.m3u8", output=tmp_path, merge=False, progress_hook=bad_hook).ok


def test_library_is_quiet_and_uses_logging(server, tmp_path, capsys, caplog):
    with caplog.at_level(logging.INFO, logger="streamdl"):
        streamdl.download(f"{server}/ok/master.m3u8", output=tmp_path, merge=False)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""              # 不接管呼叫端的終端機
    assert any("使用提取器：direct" in r.getMessage() for r in caplog.records)


def test_non_interactive_by_default(server, tmp_path):
    seen = []
    streamdl.download(f"{server}/ok/master.m3u8", output=tmp_path, merge=False,
                      progress_hook=lambda e: seen.append(log.interactive()))
    assert seen and not any(seen)


def test_unknown_option_is_rejected(server, tmp_path):
    with pytest.raises(pydantic.ValidationError):
        streamdl.download(f"{server}/ok/master.m3u8", output=tmp_path, qualty=1)    # 拼錯


def test_download_inside_running_loop_points_to_adownload():
    async def main():
        with pytest.raises(RuntimeError, match="adownload"):
            streamdl.download("https://example.com/a.m3u8")
    asyncio.run(main())


def test_concurrent_adownload_hooks_are_isolated(server, tmp_path):
    async def main():
        a, b = [], []
        await asyncio.gather(
            streamdl.adownload(f"{server}/ok/master.m3u8", output=tmp_path / "a", title="A", merge=False,
                               progress_hook=a.append),
            streamdl.adownload(f"{server}/ok/master.m3u8", output=tmp_path / "b", title="B", merge=False,
                               progress_hook=b.append),
        )
        return a, b

    a, b = asyncio.run(main())
    desc = lambda events: {e["description"] for e in events if e["type"] == "progress" and e["status"] == "started"}
    assert desc(a) == {"A"} and desc(b) == {"B"}
