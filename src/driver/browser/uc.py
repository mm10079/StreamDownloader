# uc.py
import json
import os
import re
import subprocess
from typing import Optional
from undetected_chromedriver import Chrome, ChromeOptions, find_chrome_executable

from ..base_driver import BaseBrowser, is_media_url

# uc 的 __del__ 會在已 quit 後再 quit 一次，Windows 上噴出 WinError 6；關閉一律由 close() 明確處理
Chrome.__del__ = lambda self: None


def chrome_major_version(binary: Optional[str]) -> Optional[int]:
    """讀取 Chrome 執行檔的主版本號；uc 預設下載最新 ChromeDriver，與已安裝版本不同時會無法啟動"""
    binary = binary or find_chrome_executable()
    if not binary or not os.path.isfile(binary):
        return None
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"(Get-Item -LiteralPath '{binary}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=15).stdout
        else:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.\d+\.\d+", out)
    return int(m.group(1)) if m else None


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

        # 開啟 performance log 以讀取網路請求（uc 的 add_cdp_listener 需 enable_cdp_events 才會運作，改用此方式）
        self.options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        # 啟動 uc 瀏覽器
        version = chrome_major_version(self.options.binary_location or None)
        self.driver = Chrome(options=self.options, headless=headless, use_subprocess=True, version_main=version)
        
        self._network_log.clear()

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

    def drain_media_requests(self) -> list[dict]:
        """讀出自上次呼叫後新發出的串流請求（m3u8 / mpd），含請求標頭"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        found = []
        for entry in self.driver.get_log("performance"):
            try:
                msg = json.loads(entry["message"]).get("message", {})
            except (KeyError, ValueError):
                continue
            if msg.get("method") != "Network.requestWillBeSent":
                continue
            req = msg.get("params", {}).get("request", {})
            url = req.get("url", "")
            if is_media_url(url):
                item = {"url": url, "method": req.get("method"), "headers": req.get("headers") or {},
                        "type": msg["params"].get("type"), "document_url": msg["params"].get("documentURL", "")}
                found.append(item)
                self._network_log.append(item)
        return found

    def get_network_requests(self) -> list:
        """取得網路請求紀錄 (專門用來抓取該視角的 m3u8 / mpd 串流網址)"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        # 回傳目前攔截到的所有特定請求（如 m3u8）
        return self._network_log