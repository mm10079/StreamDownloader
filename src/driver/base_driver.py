import re
from abc import ABC, abstractmethod

_MEDIA_RE = re.compile(r"\.(m3u8|mpd)(?:$|[?#/])", re.IGNORECASE)


def is_media_url(url: str) -> bool:
    """是否為串流清單網址（m3u8 / mpd）"""
    return bool(url.startswith("http") and _MEDIA_RE.search(url))

class BaseBrowser(ABC):

    # ==================== 瀏覽器生命週期 ====================
    @abstractmethod
    def start(self, headless: bool = False):
        """啟動瀏覽器獨立實例"""
        pass

    @abstractmethod
    def close(self):
        """關閉瀏覽器獨立實例，釋放記憶體"""
        pass

    # ==================== 導航與狀態 ====================
    @abstractmethod
    def goto(self, url: str):
        """導航到指定的直播視角 URL"""
        pass

    @property
    @abstractmethod
    def current_url(self) -> str:
        """取得當前實例正在播放的網址"""
        pass

    @property
    @abstractmethod
    def content(self) -> str:
        """取得當前頁面的 HTML 內容 (用於解析直播狀態)"""
        pass

    # ==================== Session / 憑證管理 ====================
    @property
    @abstractmethod
    def cookies(self) -> list[dict]:
        """取得當前實例的 Cookie 列表 (用於導出 Session)"""
        pass

    @cookies.setter
    @abstractmethod
    def cookies(self, cookies_list: list[dict]):
        """將外部 Cookie 灌入當前實例 (用於同步 Session)"""
        pass

    @property
    @abstractmethod
    def user_agent(self) -> str:
        """瀏覽器實際使用的 UA，Session 以此為準確保指紋一致"""
        pass

    @abstractmethod
    def execute_async_script(self, script: str, *args):
        """在頁面內執行非同步 JS（BrowserFetcher 用來以頁面身分下載）"""
        pass

    # ==================== 串流網路攔截 ====================
    @abstractmethod
    def drain_media_requests(self) -> list[dict]:
        """讀出自上次呼叫後新發出的串流請求：[{url, headers, document_url, ...}]"""
        pass

    @abstractmethod
    def get_network_requests(self) -> list:
        """取得網路請求紀錄 (專門用來抓取該視角的 m3u8 / mpd 串流網址)"""
        pass