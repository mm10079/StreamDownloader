from .base_driver import BaseBrowser
from enum import Enum

class BrowserType(Enum):
    """瀏覽器類型枚舉"""
    UC = "uc"

def get_browser_class(browser_type: BrowserType) -> type[BaseBrowser]:
    """根據瀏覽器類型返回對應的瀏覽器類別"""
    if browser_type == BrowserType.UC:
        from .browser.uc import UC
        return UC
    else:
        raise ValueError(f"Unsupported browser type: {browser_type}")