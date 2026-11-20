import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, List, Optional

from pydantic import BaseModel

from ..core.models import MediaJob
from ..core.options import Options
from ..session import Session, SessionManager

if TYPE_CHECKING:
    from ..driver.base_driver import BaseBrowser


class ExtractorConfig(BaseModel):
    enable: bool = True
    name: str = ""
    description: str = ""
    valid_url_regex: Optional[str] = None   # 正則匹配網址
    site_url_regex: Optional[str] = None    # 網站的任何頁面（瀏覽器監控在使用者瀏覽官方網站時呼叫 prepare_browser）
    priority: int = 100                     # 數字小者先比對；通用提取器應設大值
    tests: Optional[List] = None

    # 網站特性
    backfill: bool = True                   # 是否允許回溯（取代舊版 SKIP_FORMAT_URLS）
    need_driver: bool = False               # 是否需要啟動瀏覽器
    need_single_driver_session: bool = False  # 每個視角是否需要獨立 session（Session.fork）
    need_auth: bool = False


@dataclass
class ExtractContext:
    options: Options
    sessions: SessionManager
    browsers: list["BaseBrowser"] = field(default_factory=list)
    handoff: Optional["BaseBrowser"] = None     # 瀏覽器監控交給網站提取器沿用的瀏覽器
    stop: asyncio.Event = field(default_factory=asyncio.Event)   # 使用者按 Ctrl+C（軟停止）

    def new_session(self, headers: Optional[dict] = None) -> Session:
        base = {"User-Agent": self.options.user_agent}
        base.update(headers or {})
        return self.sessions.create(headers=base, proxy=self.options.proxy)

    async def new_browser(self, headless: Optional[bool] = None, profile: bool = False) -> "BaseBrowser":
        """開啟瀏覽器；在整個下載流程結束時由 Pipeline 統一關閉（下載期間可能仍需要它維持 session）

        profile=True：使用 --chrome-profile 的固定設定檔（保留登入與擴充功能）。
        - 有交接中的瀏覽器（瀏覽器監控 → 網站提取器）時直接沿用，不重開：登入、擴充功能與 VPN 狀態都保留
        - 同一設定檔同時只能由一個 Chrome 使用；已被占用時改用設定檔副本，並從原瀏覽器複製 cookies"""
        from pathlib import Path
        from ..driver import BrowserType, get_browser_class
        from ..utils import log
        want_headless = self.options.headless if headless is None else headless
        if profile and not want_headless and self.handoff is not None:
            browser, self.handoff = self.handoff, None
            try:
                await asyncio.to_thread(lambda: browser.current_url)
                return browser
            except Exception:
                await self.close_browser(browser)       # 使用者已關閉交接的瀏覽器 → 重開
        elif self.handoff is not None:
            browser, self.handoff = self.handoff, None
            await self.close_browser(browser)           # 提取器不沿用（例如不需固定設定檔）→ 關閉，避免殘留視窗
        try:
            browser = get_browser_class(BrowserType.UC)()
            from undetected_chromedriver import ChromeOptions
        except ImportError as e:
            raise RuntimeError("此網址需要瀏覽器功能，請安裝：pip install \"streamdl[browser]\"") from e
        if self.options.chrome_path:
            browser.options = ChromeOptions()
            browser.options.binary_location = self.options.chrome_path

        source = None       # 持有固定設定檔的瀏覽器（要從它複製 cookies）
        if profile and self.options.chrome_profile:
            from ..driver.browser.profile import clone_profile
            path = str(Path(self.options.chrome_profile).expanduser().resolve())
            source = next((b for b in self.browsers if getattr(b, "user_data_dir", None) == path), None)
            if source is None:
                Path(path).mkdir(parents=True, exist_ok=True)
                browser.user_data_dir = path
            else:
                await log.info("固定設定檔已由另一個瀏覽器使用，這個瀏覽器改用設定檔副本（含擴充功能與登入狀態）")
                browser.user_data_dir = await asyncio.to_thread(clone_profile, path)
                browser.temp_profile = True
        try:
            await asyncio.to_thread(browser.start, want_headless)
        except Exception:
            await asyncio.to_thread(browser.close)      # 刪除設定檔副本
            raise
        self.browsers.append(browser)
        if source is not None:
            try:
                cookies = await asyncio.to_thread(source.export_cookies)
                count = await asyncio.to_thread(browser.import_cookies, cookies)
                await log.debug(f"已從原瀏覽器複製 {count} 個 cookies")
            except Exception as e:
                await log.debug(f"複製 cookies 失敗：{type(e).__name__}: {e}")
        return browser

    async def close_browser(self, browser: "BaseBrowser") -> None:
        """提前關閉單一瀏覽器（例如監控用瀏覽器交接後）"""
        if browser in self.browsers:
            self.browsers.remove(browser)
        try:
            await asyncio.to_thread(browser.close)
        except Exception:
            pass

    async def close(self) -> None:
        self.handoff = None
        for browser in self.browsers:
            try:
                await asyncio.to_thread(browser.close)
            except Exception:
                pass
        self.browsers.clear()


class InfoExtractor(ABC):
    """網站適配器

    職責：解析網址 → 建立 Session（決定 cookies / headers / proxy）→ 產出 MediaJob。
    下載階段遇到 401/403 時，Session 會呼叫 refresh()，由提取器決定如何更新（重新登入、重抓簽章等）。
    """
    config: ClassVar[ExtractorConfig] = ExtractorConfig(enable=False)

    def match(self, url: str) -> bool:
        return bool(self.config.valid_url_regex and re.search(self.config.valid_url_regex, url))

    @abstractmethod
    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        ...

    def match_site(self, url: str) -> bool:
        return bool(self.config.site_url_regex and re.search(self.config.site_url_regex, url))

    async def prepare_browser(self, url: str, browser: "BaseBrowser", ctx: ExtractContext) -> None:
        """瀏覽器監控中，使用者第一次進入此網站時呼叫（例如自動登入）；預設不處理"""
        return None

    async def refresh(self, session: Session) -> None:
        """預設不處理；需要時在 extract 中呼叫 session.set_refresher(self.refresh)"""
        return None
