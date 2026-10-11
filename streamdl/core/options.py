import os
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .keys import parse_keys

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class Options(BaseModel):
    """使用者層級的設定，由 CLI 或其他前端填入，整個流程唯讀"""
    model_config = ConfigDict(extra="forbid")      # API 傳入拼錯的參數名稱時直接報錯，而不是默默忽略
    url: str = ""
    title: str = Field(default="media", description="無法從網站取得標題時使用的檔名")
    output: Path = Field(default=Path("downloads"), description="輸出資料夾")

    # 網路 / Session（只在直接下載串流網址時使用，網站提取器會自行決定）
    referer: str = Field(default="", description="Referer 標頭（直接下載串流網址時使用）")
    user_agent: str = Field(default=DEFAULT_USER_AGENT, description="User-Agent（使用瀏覽器時以瀏覽器實際值為準）")
    cookies: str = Field(default="", description="cookies 檔案路徑（Netscape 格式）或 'a=1; b=2' 字串")
    proxy: Optional[str] = Field(default=None, description="代理伺服器，例如 http://127.0.0.1:8080")

    # 下載
    fetcher: str = Field(default="httpx", description="httpx / aria2 / curl / browser")
    concurrency: int = Field(default=8, description="全域同時下載數")
    per_host: int = Field(default=6, description="單一主機同時下載數")
    retries: int = Field(default=5, description="每個片段的重試次數")
    quality: int = Field(default=0, description="畫質序號，0 為最高，數字越大畫質越低（HLS / DASH 影像軌）")
    backfill: bool = Field(default=False, description="嘗試回溯播放清單以外的較早片段")
    backfill_distance: int = Field(default=10000, description="連續序號時往回搜尋的最大距離")
    decrypt: bool = Field(default=False, description="下載中同步解密片段")
    key: str = Field(default="", description="解密金鑰（hex）：KID:KEY，不知道 KID 時可只填 KEY；多組以逗號分隔。"
                                              "也可填金鑰檔路徑（每行一組，可混用 hex / UUID / base64 / ClearKey JSON / 16 bytes 二進位）。"
                                              "不確定哪一組正確時可全部列出，會依 KID 對應或以實際片段試解自動選出")
    merge: bool = Field(default=True, description="完成後以 ffmpeg 合併")
    ffmpeg: str = Field(default="ffmpeg", description="ffmpeg 路徑（exe 版已內嵌）")

    # 直播監控
    live_idle_limit: int = Field(default=10, description="連續幾次輪詢沒有新片段就視為結束")
    live_error_limit: int = Field(default=10, description="連續幾次讀取播放清單失敗就視為結束")

    # aria2
    aria2_rpc: str = Field(default="http://localhost:6800/jsonrpc", description="aria2 RPC 位址（--fetcher aria2）")
    aria2_secret: str = Field(default="", description="aria2 RPC 密鑰")

    # 網站帳號（建議用環境變數 STREAMDL_ACCOUNT / STREAMDL_PASSWORD，避免密碼出現在指令或 .cmd 檔）
    account: str = Field(default_factory=lambda: os.environ.get("STREAMDL_ACCOUNT", ""), description="網站登入帳號")
    password: str = Field(default_factory=lambda: os.environ.get("STREAMDL_PASSWORD", ""), description="網站登入密碼")

    # 網站內容
    media: bool = Field(default=True, description="下載影音串流")
    attachment: bool = Field(default=True, description="下載附件（留言、禮物、票券資訊、圖片等）")
    skip: str = Field(default="", description="略過的網址或 ID，以逗號分隔")
    wait: bool = Field(default=True, description="直播尚未開始時等待開播")

    # 瀏覽器
    browser: Literal["auto", "always", "never"] = Field(
        default="auto", description="auto：網站需要時才開；always：以瀏覽器維持 session；never：完全不開")
    chrome_path: str = Field(default="", description="Chrome 執行檔路徑，預設自動尋找已安裝的 Chrome")
    chrome_profile: str = Field(default="", description="固定的瀏覽器設定檔資料夾：保留登入狀態與自行安裝的擴充功能（如 VPN）；"
                                                         "同一時間只能由一個瀏覽器使用")
    headless: bool = Field(default=False, description="瀏覽器無頭模式（需要手動登入或操作時無效）")
    keep_browser: bool = Field(default=True, description="全部任務結束後保持瀏覽器開啟（可繼續觀看直播），按 Enter 才關閉；"
                                                         "僅在終端機互動模式下有效")

    @field_validator("key")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        """讀取金鑰檔、辨識各種寫法，統一為「KID:KEY,KEY」（小寫 hex）；無法辨識時在建立 Options 時就報錯"""
        return ",".join(f"{kid}:{key}" if kid else key for kid, key in parse_keys(value))

    @property
    def key_list(self) -> list[tuple[str, str]]:
        """全部金鑰 [(kid, key)]，保留順序與未指定 KID 的多組金鑰"""
        return [(kid, key) for kid, _, key in (item.rpartition(":") for item in self.key.split(",") if item)]

    @property
    def key_map(self) -> dict[str, str]:
        """{kid: key}；未指定 KID 的金鑰放在 "" """
        return dict(self.key_list)

    @property
    def skip_set(self) -> set[str]:
        return {s.strip() for s in self.skip.split(",") if s.strip()}
