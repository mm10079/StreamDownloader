# uc.py
import json
from typing import Optional
from undetected_chromedriver import Chrome, ChromeOptions

from ..base_driver import BaseBrowser

class UC(BaseBrowser):
    def __init__(self):
        self.driver: Optional[Chrome] = None
        self.options: Optional[ChromeOptions] = None
        self._network_log: list = []  # 用於儲存 CDP 攔截到的網路請求

    def start(self, headless: bool = False):
        """啟動瀏覽器獨立實例"""
        if self.driver is not None:
            return  # 已經啟動過了
            
        if self.options is None:
            self.options = ChromeOptions()
            
        # 優化直播專用參數：允許自動播放音訊、停用背景降頻
        self.options.add_argument("--autoplay-policy=no-user-gesture-required")
        self.options.add_argument("--disable-background-timer-throttling")
        self.options.add_argument("--disable-backgrounding-occluded-windows")
        self.options.add_argument("--disable-renderer-backgrounding")

        # 啟動 uc 瀏覽器
        self.driver = Chrome(options=self.options, headless=headless, use_subprocess=True)
        
        # 💡 【核心補足】開啟 Chrome DevTools Protocol (CDP) 監聽網路請求
        self._network_log.clear()
        self.driver.execute_cdp_cmd("Network.enable", {})
        
        # 當有新的網路請求發出或收到回應時，觸發此回呼函式
        def cb(message):
            try:
                # 解析 CDP 傳回的 JSON 數據
                current_log = json.loads(message)
                # 我們通常只需要 RequestWillBeSent (即將發送的請求)
                if current_log.get("method") == "Network.requestWillBeSent":
                    request_data = current_log["params"]["request"]
                    url = request_data.get("url", "")
                    
                    # 篩選你想要的直播關鍵字，避免 memory leak 存太多無用請求
                    if "m3u8" in url or "mpd" in url or "playlist" in url:
                        self._network_log.append({
                            "url": url,
                            "method": request_data.get("method"),
                            "headers": request_data.get("headers"),
                            "type": current_log["params"].get("type")
                        })
            except Exception:
                pass

        # 將監聽器綁定到 Chrome 的日誌回呼
        self.driver.add_cdp_listener("Network.requestWillBeSent", cb)

    def close(self):
        """關閉瀏覽器獨立實例，釋放記憶體"""
        if self.driver is not None:
            self.driver.quit()
            self.driver = None
        self._network_log.clear()

    def goto(self, url: str):
        """導航到指定的直播視角 URL"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        self.driver.get(url)

    @property
    def current_url(self) -> str:
        """取得當前實例正在播放的網址"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        return self.driver.current_url
    
    @property
    def content(self) -> str:
        """取得當前頁面的 HTML 內容 (用於解析直播狀態)"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        return self.driver.page_source
    
    @property
    def cookies(self) -> list[dict]:
        """取得當前實例的 Cookie 列表 (用於導出 Session)"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        return self.driver.get_cookies()
    
    @cookies.setter
    def cookies(self, cookies_list: list[dict]):
        """將外部 Cookie 灌入當前實例 (用於同步 Session)"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        
        self.driver.delete_all_cookies()
        for cookie in cookies_list:
            # 修正部分網站 cookie 帶有 expiry 導致的格式問題
            if 'expiry' in cookie:
                try:
                    cookie['expiry'] = int(cookie['expiry'])
                except (ValueError, TypeError):
                    cookie.pop('expiry', None) # 若格式真的壞掉就移除，免得噴錯
            try:
                self.driver.add_cookie(cookie)
            except Exception as e:
                # 有些網域不符的 Cookie 注入會失敗，可選擇 log 紀錄或跳過
                pass

    @property
    def user_agent(self) -> str:
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        return self.driver.execute_script("return navigator.userAgent")

    def execute_async_script(self, script: str, *args):
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        return self.driver.execute_async_script(script, *args)

    def get_network_requests(self) -> list:
        """取得網路請求紀錄 (專門用來抓取該視角的 m3u8 / mpd 串流網址)"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        # 回傳目前攔截到的所有特定請求（如 m3u8）
        return self._network_log