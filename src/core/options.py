import os
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, Field

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class Options(BaseModel):
    """使用者層級的設定，由 CLI 或其他前端填入，整個流程唯讀"""
    url: str = ""
    title: str = Field(default="media", description="無法從網站取得標題時使用的檔名")
    output: Path = Field(default=Path("downloads"), description="輸出資料夾")

    # 網路 / Session（只在直接下載串流網址時使用，網站提取器會自行決定）
    referer: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    cookies: str = Field(default="", description="cookies 檔案路徑（Netscape 格式）或 'a=1; b=2' 字串")
    proxy: Optional[str] = None

    # 下載
    fetcher: str = Field(default="httpx", description="httpx / aria2 / curl / browser")
    concurrency: int = Field(default=8, description="全域同時下載數")
    per_host: int = Field(default=6, description="單一主機同時下載數")
    retries: int = 5
    quality: int = Field(default=0, description="0 為最高畫質，數字越大畫質越低")
    backfill: bool = Field(default=True, description="嘗試回溯播放清單以外的較早片段")
    backfill_distance: int = Field(default=10000, description="連續序號時往回搜尋的最大距離")
    decrypt: bool = Field(default=False, description="下載中同步解密片段")
    merge: bool = Field(default=True, description="完成後以 ffmpeg 合併")
    ffmpeg: str = "ffmpeg"

    # 直播監控
    live_idle_limit: int = Field(default=10, description="連續幾次輪詢沒有新片段就視為結束")
    live_error_limit: int = Field(default=10, description="連續幾次讀取播放清單失敗就視為結束")

    # aria2
    aria2_rpc: str = "http://localhost:6800/jsonrpc"
    aria2_secret: str = ""

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
    chrome_path: str = ""
    headless: bool = False

    @property
    def skip_set(self) -> set[str]:
        return {s.strip() for s in self.skip.split(",") if s.strip()}
