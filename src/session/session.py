"""Session：一個「網路身分」。

設計原則
- CookieJar 是唯一的 cookie 來源。httpx client 直接共用同一個 jar（不是複製），
  伺服器 Set-Cookie 換發的值會立刻反映給所有請求。
- 外部工具（aria2 / curl / 瀏覽器內 fetch）需要 cookie 時，用 headers_for(url) 即時產生，
  不保存副本。
- authority 指定「誰是權威」：jar 或 browser。browser 為權威時只會從瀏覽器讀回 jar，
  不會反向灌入，避免舊版雙向互灌造成的覆蓋問題。
- refresh() 為 single-flight：多個片段同時 403 時只會真正刷新一次。
"""
import asyncio
import uuid
from enum import Enum
from http.cookiejar import Cookie, CookieJar
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Optional

import httpx

from . import cookies as cookie_tools

if TYPE_CHECKING:
    from ..driver.base_driver import BaseBrowser

Refresher = Callable[["Session"], Awaitable[None]]


class Authority(str, Enum):
    JAR = "jar"
    BROWSER = "browser"


class Session:
    def __init__(
        self,
        session_id: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        proxy: Optional[str] = None,
        timeout: float = 30,
    ):
        self.id = session_id or uuid.uuid4().hex[:8]
        self.jar = CookieJar()
        self.headers: dict[str, str] = dict(headers or {})
        self.proxy = proxy
        self.timeout = timeout
        self.authority = Authority.JAR
        self.browser: Optional["BaseBrowser"] = None
        self.version = 0

        self._refresher: Optional[Refresher] = None
        self._refresh_lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    # ---------------- cookies / headers ----------------

    def set_cookies(self, items: Iterable[Cookie]) -> None:
        for c in items:
            self.jar.set_cookie(c)

    def replace_cookies(self, items: Iterable[Cookie]) -> None:
        self.jar.clear()
        self.set_cookies(items)

    def set_header(self, name: str, value: str) -> None:
        """修改標頭；已建立的 client 同步更新"""
        self.headers[name] = value
        if self._client is not None:
            self._client.headers[name] = value

    @property
    def user_agent(self) -> str:
        return self.headers.get("User-Agent", "")

    def headers_for(self, url: str, include_cookie: bool = True) -> dict[str, str]:
        """給外部工具使用的完整請求標頭"""
        headers = dict(self.headers)
        if include_cookie:
            cookie = cookie_tools.cookie_header(self.jar, url)
            if cookie:
                headers["Cookie"] = cookie
        return headers

    def export_netscape(self) -> str:
        return cookie_tools.to_netscape(self.jar)

    # ---------------- httpx ----------------

    def client(self) -> httpx.AsyncClient:
        """所有 httpx 請求共用的 client；cookies 與 session 共用同一個 jar"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self.headers,
                cookies=self.jar,          # 傳入 CookieJar 本體 → 共用而非複製
                proxy=self.proxy,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------- browser ----------------

    async def attach_browser(self, browser: "BaseBrowser", authority: bool = True) -> None:
        """綁定瀏覽器；UA 以瀏覽器實際值為準，確保爬蟲與下載的指紋一致"""
        self.browser = browser
        ua = await asyncio.to_thread(lambda: browser.user_agent)
        if ua:
            self.set_header("User-Agent", ua)
        if authority:
            self.authority = Authority.BROWSER
            await self.pull_from_browser()

    async def pull_from_browser(self) -> None:
        if self.browser is None:
            return
        raw = await asyncio.to_thread(lambda: self.browser.cookies)
        self.replace_cookies(cookie_tools.from_browser(raw))

    async def push_to_browser(self) -> None:
        """只在 jar 為權威時允許（例如 API 登入後讓瀏覽器沿用登入狀態）"""
        if self.browser is None:
            return
        if self.authority is Authority.BROWSER:
            raise RuntimeError("browser 為權威時不可反向注入 cookies")
        data = cookie_tools.to_browser(self.jar)
        await asyncio.to_thread(setattr, self.browser, "cookies", data)

    # ---------------- refresh ----------------

    def set_refresher(self, refresher: Optional[Refresher]) -> None:
        """由提取器提供：例如重新登入、重新取得簽章網址"""
        self._refresher = refresher

    async def refresh(self, seen_version: int) -> bool:
        """seen_version 為呼叫者發出失敗請求時看到的 version。
        若期間已有其他人刷新過，直接回傳 True 讓呼叫者重試。"""
        async with self._refresh_lock:
            if self.version != seen_version:
                return True
            if self._refresher is not None:
                await self._refresher(self)
            elif self.authority is Authority.BROWSER:
                await self.pull_from_browser()
            else:
                return False
            self.version += 1
            return True

    # ---------------- fork ----------------

    def fork(self, session_id: Optional[str] = None) -> "Session":
        """複製一份獨立身分（多視角各自需要 session 時使用）；之後兩者互不影響"""
        other = Session(session_id, headers=self.headers, proxy=self.proxy, timeout=self.timeout)
        other.set_cookies(list(self.jar))
        other._refresher = self._refresher
        return other


class SessionManager:
    """Session 註冊表；資料模型只存 session_id，由這裡取回實體"""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def create(self, **kwargs) -> Session:
        session = Session(**kwargs)
        self._sessions[session.id] = session
        return session

    def add(self, session: Session) -> Session:
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"未知的 session: {session_id}") from None

    def fork(self, session_id: str) -> Session:
        return self.add(self.get(session_id).fork())

    async def aclose(self) -> None:
        for s in self._sessions.values():
            await s.aclose()
