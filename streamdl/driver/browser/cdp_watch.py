"""以 CDP 監聽所有目標的網路請求（分頁、跨網站 iframe、Web Worker）

chromedriver 的 performance log 只記錄主頁面這個目標的網路事件：跨網站 iframe（獨立處理程序）
與 Worker 內發出的請求不會出現，嵌入式播放器（例如 ULIZA、hls.js 的 Worker 模式）的 m3u8 因此抓不到。

做法：另外連上瀏覽器層級的 DevTools websocket
1. Target.setDiscoverTargets：得知新開的分頁並附加（flatten 模式，事件都走同一條連線）
2. 每個附加的工作階段：Network.enable + Target.setAutoAttach(waitForDebuggerOnStart=True)
   → 子目標（iframe / Worker，含巢狀）出現時先暫停，啟用 Network 後才 Runtime.runIfWaitingForDebugger，
     避免漏掉子目標最早發出的請求
3. Network.requestWillBeSent 中符合條件的網址放入佇列，由 drain() 取出
"""
import json
import threading
import urllib.request
from collections import deque
from typing import Callable, Optional

import websocket   # websocket-client（selenium 的依賴）

ATTACHABLE = {"page", "iframe", "worker", "shared_worker", "service_worker"}


class CdpMediaWatcher:
    def __init__(self, debugger_address: str, accept: Callable[[str], bool]):
        self.address = debugger_address
        self.accept = accept
        self._ws: Optional[websocket.WebSocket] = None
        self._thread: Optional[threading.Thread] = None
        self._found: deque[dict] = deque()
        self._lock = threading.Lock()
        self._next_id = 0
        self._attached: set[str] = set()        # 已附加的 targetId
        self._running = False

    # ---------------- 生命週期 ----------------
    def start(self, timeout: float = 10) -> None:
        with urllib.request.urlopen(f"http://{self.address}/json/version", timeout=timeout) as resp:
            ws_url = json.load(resp)["webSocketDebuggerUrl"]
        # suppress_origin：Chrome 111+ 預設拒絕帶 Origin 標頭的 DevTools 連線
        self._ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._ws.settimeout(1)
        self._running = True
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._thread = threading.Thread(target=self._loop, name="cdp-media-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    @property
    def alive(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self._found)
            self._found.clear()
        return items

    # ---------------- CDP ----------------
    def _send(self, method: str, params: Optional[dict] = None, session: Optional[str] = None) -> None:
        self._next_id += 1
        msg = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._ws.send(json.dumps(msg))

    def _attach_page(self, target_id: str) -> None:
        if target_id in self._attached:
            return
        self._attached.add(target_id)
        self._send("Target.attachToTarget", {"targetId": target_id, "flatten": True})

    def _setup_session(self, session: str, paused: bool) -> None:
        self._send("Network.enable", {}, session)
        self._send("Target.setAutoAttach",
                   {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, session)
        if paused:
            self._send("Runtime.runIfWaitingForDebugger", {}, session)

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.targetCreated":
            info = params.get("targetInfo", {})
            if info.get("type") == "page":
                self._attach_page(info["targetId"])
        elif method == "Target.attachedToTarget":
            info = params.get("targetInfo", {})
            session = params.get("sessionId")
            if session and info.get("type") in ATTACHABLE:
                self._attached.add(info.get("targetId", ""))
                self._setup_session(session, paused=params.get("waitingForDebugger", False))
            elif session and params.get("waitingForDebugger"):
                self._send("Runtime.runIfWaitingForDebugger", {}, session)     # 不監聽的類型也不能卡住
        elif method == "Target.targetDestroyed":
            self._attached.discard(params.get("targetId", ""))
        elif method == "Network.requestWillBeSent":
            request = params.get("request", {})
            url = request.get("url", "")
            if self.accept(url):
                with self._lock:
                    self._found.append({
                        "url": url, "method": request.get("method"), "headers": request.get("headers") or {},
                        "type": params.get("type"), "document_url": params.get("documentURL", ""),
                    })

    def _loop(self) -> None:
        while self._running:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break           # 瀏覽器關閉或連線中斷
            try:
                self._handle(json.loads(raw))
            except Exception:
                continue        # 單一事件解析失敗不影響監聽
        self._running = False
