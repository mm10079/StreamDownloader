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


_VERSION_NAME = re.compile(r"^(\d+)\.\d+\.\d+\.\d+(?:\.manifest)?$")


def _version_from_folder(binary: str) -> Optional[int]:
    """Windows 的 Chrome 旁有以版本命名的資料夾（例如 157.0.8103.79），攜帶版則是 157.0.8103.79.manifest；
    不需執行任何程式。更新後新舊版本資料夾可能並存，取最大者"""
    try:
        names = os.listdir(os.path.dirname(binary))
    except OSError:
        return None
    majors = [int(m.group(1)) for n in names if (m := _VERSION_NAME.match(n))]
    return max(majors) if majors else None


def _powershell() -> str:
    """以絕對路徑呼叫，避免 PATH 中沒有 PowerShell 時讀不到版本"""
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")
    return path if os.path.isfile(path) else "powershell"


def chrome_major_version(binary: Optional[str]) -> Optional[int]:
    """讀取 Chrome 執行檔的主版本號；uc 預設下載最新 ChromeDriver，與已安裝版本不同時會無法啟動"""
    binary = binary or find_chrome_executable()
    if not binary or not os.path.isfile(binary):
        return None
    if os.name == "nt" and (major := _version_from_folder(binary)):
        return major
    try:
        if os.name == "nt":
            out = subprocess.run(
                [_powershell(), "-NoProfile", "-Command", f"(Get-Item -LiteralPath '{binary}').VersionInfo.ProductVersion"],
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
        self.user_data_dir: Optional[str] = None    # 固定設定檔（保留登入與擴充功能）；None 為暫存設定檔
        self.temp_profile = False                   # user_data_dir 為設定檔副本，關閉時刪除
        self._label_script: Optional[str] = None    # 標題標記的 CDP 腳本 identifier
        self._watcher = None                        # CdpMediaWatcher
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
        try:
            self.driver = Chrome(options=self.options, headless=headless, use_subprocess=True, version_main=version,
                                 user_data_dir=self.user_data_dir)
        except Exception as e:
            if self.user_data_dir:
                raise RuntimeError(f"無法以設定檔啟動瀏覽器（{self.user_data_dir}），"
                                   f"請確認沒有其他 Chrome 視窗正在使用這個設定檔：{e}") from e
            raise
        # execute_async_script 的頁面內等待（例如等待 React 表單可操作）可能長達數十秒
        self.driver.set_script_timeout(60)
        
        self._network_log.clear()
        self._start_watcher()

    def _start_watcher(self) -> None:
        """CDP 監聽跨網站 iframe 與 Worker 的請求（performance log 看不到）；失敗時退回只用 performance log"""
        try:
            from .cdp_watch import CdpMediaWatcher
            self._watcher = CdpMediaWatcher(self.driver.options.debugger_address, is_media_url)
            self._watcher.start()
        except Exception:
            self._watcher = None

    def close(self):
        """關閉瀏覽器獨立實例，釋放記憶體"""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self.driver is not None:
            self.driver.quit()
            self.driver = None
        self._network_log.clear()
        if self.temp_profile and self.user_data_dir:
            from .profile import remove_profile
            remove_profile(self.user_data_dir)
            self.temp_profile = False

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

    # 在分頁標題（即視窗標題）前加上【標記】；頁面換頁或 SPA 改標題後每秒補回
    _LABEL_JS = """(() => {
  const tag = %s;
  const apply = () => {
    const base = (document.title || '').replace(/^【[^】]*】\\s*/, '');
    if (document.title !== tag + base) document.title = tag + base;
  };
  apply();
  document.addEventListener('DOMContentLoaded', apply);
  setInterval(apply, 1000);
})();"""

    def set_label(self, label: str) -> None:
        """標記這個瀏覽器（例如視角名稱），使用者才分得出哪個視窗要做什麼"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        script = self._LABEL_JS % json.dumps(f"【{label}】 ")
        if self._label_script:
            try:
                self.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument",
                                            {"identifier": self._label_script})
            except Exception:
                pass
        result = self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        self._label_script = result.get("identifier")
        try:
            self.driver.execute_script(script)      # 目前已開啟的頁面
        except Exception:
            pass

    # 可傳給 Network.setCookie 的欄位（Storage.getCookies 回傳的其他欄位如 size、session 不接受）
    _COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires",
                      "priority", "sourceScheme", "sourcePort")

    def export_cookies(self) -> list[dict]:
        """所有網域的 cookies（含 httpOnly），CDP 格式"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        try:
            return self.driver.execute_cdp_cmd("Storage.getCookies", {}).get("cookies", [])
        except Exception:
            return self.driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])

    def import_cookies(self, cookies: list[dict]) -> int:
        """寫入 export_cookies 的結果，回傳成功筆數"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        params = []
        for c in cookies:
            if c.get("partitionKey"):
                continue        # 分區 cookie 的格式隨版本變動，略過
            item = {k: c[k] for k in self._COOKIE_FIELDS if k in c}
            if c.get("session") or not item.get("expires") or item["expires"] <= 0:
                item.pop("expires", None)
            params.append(item)
        try:
            self.driver.execute_cdp_cmd("Network.setCookies", {"cookies": params})
            return len(params)
        except Exception:
            pass
        ok = 0
        for item in params:     # 整批失敗時逐筆寫入，略過格式不符的
            try:
                self.driver.execute_cdp_cmd("Network.setCookie", item)
                ok += 1
            except Exception:
                pass
        return ok

    def drain_media_requests(self) -> list[dict]:
        """讀出自上次呼叫後新發出的串流請求（m3u8 / mpd），含請求標頭"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        found = []
        watching = self._watcher is not None and self._watcher.alive
        for entry in self.driver.get_log("performance"):    # 監聽器運作時仍要讀出，避免紀錄累積占用記憶體
            if watching:
                continue
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
        if watching:
            found = self._watcher.drain()      # 涵蓋主頁面、iframe（含跨網站）與 Worker
        self._network_log.extend(found)
        return found

    def get_network_requests(self) -> list:
        """取得網路請求紀錄 (專門用來抓取該視角的 m3u8 / mpd 串流網址)"""
        if self.driver is None:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 start() 方法")
        # 回傳目前攔截到的所有特定請求（如 m3u8）
        return self._network_log