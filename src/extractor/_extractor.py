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

    def new_session(self, headers: Optional[dict] = None) -> Session:
        base = {"User-Agent": self.options.user_agent}
        base.update(headers or {})
        return self.sessions.create(headers=base, proxy=self.options.proxy)

    async def new_browser(self, headless: Optional[bool] = None) -> "BaseBrowser":
        """開啟瀏覽器；在整個下載流程結束時由 Pipeline 統一關閉（下載期間可能仍需要它維持 session）"""
        from ..driver import BrowserType, get_browser_class
        browser = get_browser_class(BrowserType.UC)()
        if self.options.chrome_path:
            from undetected_chromedriver import ChromeOptions
            browser.options = ChromeOptions()
            browser.options.binary_location = self.options.chrome_path
        await asyncio.to_thread(browser.start, self.options.headless if headless is None else headless)
        self.browsers.append(browser)
        return browser

    async def close(self) -> None:
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

    async def refresh(self, session: Session) -> None:
        """預設不處理；需要時在 extract 中呼叫 session.set_refresher(self.refresh)"""
        return None
