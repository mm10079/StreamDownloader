"""Fetcher：只負責「把一個網址抓成一個檔案」。

協定邏輯（HLS / DASH）不在這裡；換成 aria2、curl 等工具時協定層不需修改。
所有 Fetcher 都從 Session 取得 cookies / headers / proxy，不自行保存。
"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from ..session import Session, SessionManager

AUTH_STATUS = {401, 403}


@dataclass
class FetchRequest:
    url: str
    dest: Path
    session_id: str
    byte_range: Optional[tuple[int, int]] = None     # 含頭尾，與 HTTP Range 相同
    headers: dict[str, str] = field(default_factory=dict)  # 額外標頭（覆蓋 session）


@dataclass
class FetchResult:
    ok: bool
    status: Optional[int] = None
    size: int = 0
    error: str = ""

    @property
    def auth_error(self) -> bool:
        return self.status in AUTH_STATUS


class Fetcher(ABC):
    name: str = "base"
    capabilities: frozenset[str] = frozenset()   # 例如 {"range", "resume"}

    def __init__(self, sessions: SessionManager, concurrency: int = 8, per_host: int = 6):
        self.sessions = sessions
        self._global = asyncio.Semaphore(concurrency)
        self._per_host_limit = per_host
        self._hosts: dict[str, asyncio.Semaphore] = {}

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc
        if host not in self._hosts:
            self._hosts[host] = asyncio.Semaphore(self._per_host_limit)
        return self._hosts[host]

    async def fetch(self, req: FetchRequest) -> FetchResult:
        """並行控制集中在這裡，子類別只實作 _fetch"""
        async with self._global, self._host_sem(req.url):
            req.dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                return await self._fetch(req, self.sessions.get(req.session_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return FetchResult(ok=False, error=f"{type(e).__name__}: {e}")

    @abstractmethod
    async def _fetch(self, req: FetchRequest, session: Session) -> FetchResult:
        ...

    async def aclose(self) -> None:
        pass

    @staticmethod
    def part_path(dest: Path) -> Path:
        return dest.with_name(dest.name + ".part")


async def fetch_with_retry(fetcher: Fetcher, req: FetchRequest, retries: int = 5, backoff: float = 2.0) -> FetchResult:
    """失敗重試；遇到 401/403 先請 Session 刷新（single-flight）再重試"""
    session = fetcher.sessions.get(req.session_id)
    result = FetchResult(ok=False, error="not started")
    for attempt in range(1, retries + 1):
        seen = session.version
        result = await fetcher.fetch(req)
        if result.ok:
            return result
        if result.auth_error:
            if not await session.refresh(seen):
                return result       # 無法刷新，重試也沒有意義
        elif result.status == 404 and attempt >= 2:
            return result
        await asyncio.sleep(min(backoff * attempt, 15))
    return result


async def request_text(session: Session, url: str, retries: int = 3) -> tuple[Optional[str], Optional[int]]:
    """讀取播放清單等小型文字資源；回傳 (內容, 狀態碼)"""
    status: Optional[int] = None
    for attempt in range(1, retries + 1):
        seen = session.version
        try:
            resp = await session.client().get(url)
            status = resp.status_code
            if resp.status_code == 200:
                return resp.text, status
            if resp.status_code in AUTH_STATUS:
                if not await session.refresh(seen):
                    return None, status
                continue
        except httpx.HTTPError:
            status = None
        await asyncio.sleep(attempt)
    return None, status


def create_fetcher(name: str, sessions: SessionManager, options) -> Fetcher:
    """依名稱建立 Fetcher；延遲匯入避免未使用的工具相依"""
    kwargs = dict(sessions=sessions, concurrency=options.concurrency, per_host=options.per_host)
    if name == "httpx":
        from .httpx_fetcher import HttpxFetcher
        return HttpxFetcher(**kwargs)
    if name == "aria2":
        from .aria2_fetcher import Aria2Fetcher
        return Aria2Fetcher(rpc_url=options.aria2_rpc, secret=options.aria2_secret, **kwargs)
    if name == "curl":
        from .curl_fetcher import CurlFetcher
        return CurlFetcher(**kwargs)
    if name == "browser":
        from .browser_fetcher import BrowserFetcher
        return BrowserFetcher(**kwargs)
    raise ValueError(f"未知的 fetcher: {name}")
