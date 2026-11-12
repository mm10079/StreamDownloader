import json

import pytest

pytest.importorskip("websocket")        # websocket-client 隨 streamdl[browser] 安裝

from streamdl.driver.base_driver import is_media_url
from streamdl.driver.browser.cdp_watch import CdpMediaWatcher


class FakeWs:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(json.loads(text))


def make():
    w = CdpMediaWatcher("127.0.0.1:0", is_media_url)
    w._ws = FakeWs()
    return w


def sent(w, session=None):
    return [(m["method"], m.get("sessionId")) for m in w._ws.sent if session is None or m.get("sessionId") == session]


def test_new_page_is_attached_once():
    w = make()
    info = {"targetInfo": {"targetId": "T1", "type": "page"}}
    w._handle({"method": "Target.targetCreated", "params": info})
    w._handle({"method": "Target.targetCreated", "params": info})
    assert sent(w) == [("Target.attachToTarget", None)]


def test_child_target_enabled_before_resume():
    """iframe / Worker 附加時處於暫停：必須先 Network.enable，才 runIfWaitingForDebugger"""
    w = make()
    w._handle({"method": "Target.attachedToTarget", "params": {
        "sessionId": "S2", "waitingForDebugger": True, "targetInfo": {"targetId": "W1", "type": "worker"}}})
    assert sent(w, "S2") == [("Network.enable", "S2"), ("Target.setAutoAttach", "S2"),
                             ("Runtime.runIfWaitingForDebugger", "S2")]
    auto = next(m for m in w._ws.sent if m["method"] == "Target.setAutoAttach")
    assert auto["params"] == {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}


def test_unwatched_target_type_is_still_resumed():
    w = make()
    w._handle({"method": "Target.attachedToTarget", "params": {
        "sessionId": "S3", "waitingForDebugger": True, "targetInfo": {"targetId": "X", "type": "auction_worklet"}}})
    assert sent(w, "S3") == [("Runtime.runIfWaitingForDebugger", "S3")]   # 不監聽也不能讓它一直暫停


def test_media_requests_collected_from_any_session():
    w = make()
    for sid, url in (("S1", "https://cdn/x/prog-index.m3u8?expires=1&signature=a"),
                     ("S2", "https://cdn/app.js"), ("S3", "https://cdn/v/manifest.mpd")):
        w._handle({"method": "Network.requestWillBeSent", "sessionId": sid, "params": {
            "request": {"url": url, "method": "GET", "headers": {"Referer": "https://player/"}},
            "type": "XHR", "documentURL": "https://player/embed"}})
    got = w.drain()
    assert [g["url"].rsplit("/", 1)[-1] for g in got] == ["prog-index.m3u8?expires=1&signature=a", "manifest.mpd"]
    assert got[0]["headers"]["Referer"] == "https://player/" and got[0]["document_url"] == "https://player/embed"
    assert w.drain() == []
