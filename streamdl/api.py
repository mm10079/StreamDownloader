"""對外 API

    import streamdl

    result = streamdl.download("https://example.com/master.m3u8", output="downloads", title="標題")
    if result.ok:
        print(result.files)

    # 進度：progress_hook 收到 dict 事件（格式見 streamdl.utils.reporters）
    streamdl.download(url, progress_hook=lambda e: print(e))

    # 已在事件迴圈中（例如 FastAPI、discord.py）
    result = await streamdl.adownload(url, output="downloads")

所有選項與 CLI 參數相同（底線取代連字號），例如 quality=1、backfill=True、key="KID:KEY"。

當作套件使用時：
- 不會印出 Rich 畫面：訊息寫入 logging（logger 名稱 "streamdl"），可另外傳入 progress_hook
- 不會在終端機詢問任何輸入（interactive=False）：需要的帳密請以 account / password 傳入
- 下載完成後立即關閉瀏覽器（不等待 Enter）
"""
import asyncio
from typing import Any, Optional

from .core.options import Options
from .core.pipeline import Pipeline
from .core.result import DownloadResult
from .utils import log
from .utils.console import CURRENT_PUSHER
from .utils.reporters import HookPusher, ProgressHook


async def adownload(
    url: str,
    *,
    progress_hook: Optional[ProgressHook] = None,
    interactive: bool = False,
    stop: Optional[asyncio.Event] = None,
    **options: Any,
) -> DownloadResult:
    """下載網址（串流網址或支援的網站頁面），回傳結構化結果

    progress_hook：接收進度與訊息事件的函式（在事件迴圈中同步呼叫，請勿長時間阻塞）
    interactive：是否允許在終端機詢問（帳密、標題、下載後保留瀏覽器）；預設否
    stop：設定後會停止追蹤直播並完成進行中的下載（相當於 CLI 的第一次 Ctrl+C）
    options：與 CLI 相同的設定，例如 output="downloads"、title="…"、quality=0
    """
    options.setdefault("keep_browser", interactive)
    opts = Options(url=url, **options)          # 參數名稱拼錯會在這裡拋出 ValidationError
    pusher_token = CURRENT_PUSHER.set(HookPusher(progress_hook) if progress_hook else None)
    interactive_token = log.INTERACTIVE.set(interactive)
    try:
        return await Pipeline(opts, stop).run(url)
    finally:
        CURRENT_PUSHER.reset(pusher_token)
        log.INTERACTIVE.reset(interactive_token)


def download(url: str, **kwargs: Any) -> DownloadResult:
    """adownload 的同步版本；在已執行中的事件迴圈內請改用 `await adownload(...)`"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(adownload(url, **kwargs))
    raise RuntimeError("目前已在事件迴圈中，請改用 `await streamdl.adownload(...)`")
