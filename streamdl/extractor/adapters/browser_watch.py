"""瀏覽器監控提取器（舊版 nonspecific + AdapterDispatchCenter）

沒有輸入網址、或網址沒有專用提取器時使用：
1. 開啟瀏覽器，使用者自行登入、前往播放頁面並開始播放
2. 持續檢查瀏覽器目前的網址：符合網站專用提取器（例如 ZAN-LIVE）時，交給該提取器處理
3. 同時監控瀏覽器發出的 m3u8 / mpd 請求，逐一詢問是否下載
4. 下載使用的 Session 以這個瀏覽器為 cookie 權威，下載期間瀏覽器保持開啟並定期同步
"""
import asyncio
from typing import Optional
from urllib.parse import urlsplit

from ...core.models import MediaJob, StreamKind, StreamSpec
from ...session import Session
from ...utils import log
from ...utils.paths import sanitize_filename
from .._extractor import ExtractContext, ExtractorConfig, InfoExtractor
from .direct import DirectExtractor

START_PAGE = "https://www.google.com/"
POLL = 1.0
SYNC_INTERVAL = 10
# 從瀏覽器請求中沿用的標頭（其餘如 Cookie、UA 由 Session 管理）
PASS_HEADERS = {"referer", "origin", "authorization"}

_TITLE_SCRIPT = "arguments[arguments.length - 1](document.title || '');"


class BrowserWatchError(Exception):
    pass


class BrowserWatchExtractor(InfoExtractor):
    config = ExtractorConfig(
        name="browser",
        description="開啟瀏覽器，監控頁面中的 m3u8 / mpd",
        priority=2000,          # 所有提取器都不符合時才使用
        need_driver=True,
    )

    def match(self, url: str) -> bool:
        return not url or url.startswith("http")

    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        if ctx.options.browser == "never":
            raise BrowserWatchError("沒有符合此網址的提取器，且 --browser never 不允許開啟瀏覽器")
        browser = await ctx.new_browser(headless=False, profile=True)
        await asyncio.to_thread(browser.goto, url or START_PAGE)
        await log.info("已開啟瀏覽器：請前往播放頁面並開始播放。偵測到串流時會在此詢問是否下載（Ctrl+C 結束）")

        checked_pages: set[str] = set()
        seen: set[str] = set()
        while not ctx.stop.is_set():
            try:
                current = await asyncio.to_thread(lambda: browser.current_url)
                requests = await asyncio.to_thread(browser.drain_media_requests)
            except Exception as e:
                raise BrowserWatchError(f"瀏覽器已關閉或無回應：{type(e).__name__}") from e

            # 1. 頁面符合網站專用提取器 → 交接
            if current not in checked_pages:
                checked_pages.add(current)
                site = self._site_extractor(current)
                if site is not None:
                    await log.info(f"偵測到 {site.config.name} 頁面，交由專用提取器處理：{current}")
                    # 關閉監控用瀏覽器：避免殘留空白視窗、與專用提取器的直播間 session 互相干擾，並釋放固定設定檔
                    await ctx.close_browser(browser)
                    return await site.extract(current, ctx)

            # 2. 新的串流請求 → 詢問
            accepted = []
            for req in requests:
                media = req["url"]
                if media in seen or DirectExtractor.detect_kind(media) not in (StreamKind.HLS, StreamKind.DASH):
                    continue
                seen.add(media)
                await log.info(f"找到串流：{media}")
                if await log.confirm("下載這個串流？"):
                    accepted.append(req)
                else:
                    await log.info("已略過")
            if accepted:
                return await self._build_job(ctx, browser, current, accepted)
            await asyncio.sleep(POLL)

        await log.info("已停止監控")
        return MediaJob()

    @staticmethod
    def _site_extractor(url: str) -> Optional[InfoExtractor]:
        from .. import load_extractors
        for ext in load_extractors():
            if ext.config.priority >= DirectExtractor.config.priority:
                continue    # 只交接給網站專用提取器
            if ext.match(url):
                return ext
        return None

    async def _build_job(self, ctx: ExtractContext, browser, page_url: str, accepted: list[dict]) -> MediaJob:
        opts = ctx.options
        title = opts.title
        if title == "media":
            try:
                title = await asyncio.to_thread(browser.execute_async_script, _TITLE_SCRIPT) or title
            except Exception:
                pass
        title = sanitize_filename(title)

        job = MediaJob(title=title)
        for i, req in enumerate(accepted):
            session = await self._session_for(ctx, browser, page_url, req)
            name = title if len(accepted) == 1 else f"{title}_{i + 1}"
            job.streams.append(StreamSpec(
                kind=DirectExtractor.detect_kind(req["url"]), url=req["url"], title=name,
                session_id=session.id, quality=opts.quality, backfill=opts.backfill,
                extras={"page": page_url},
            ))
        await log.info(f"開始下載 {len(job.streams)} 個串流；下載期間請保持瀏覽器開啟")
        return job

    async def _session_for(self, ctx: ExtractContext, browser, page_url: str, req: dict) -> Session:
        """瀏覽器為權威；Referer / Origin 等沿用播放器實際送出的值（播放器可能在 iframe 內）"""
        headers = {k: v for k, v in (req.get("headers") or {}).items() if k.lower() in PASS_HEADERS}
        if not any(k.lower() == "referer" for k in headers):
            headers["Referer"] = req.get("document_url") or page_url
        if not any(k.lower() == "origin" for k in headers):
            parts = urlsplit(headers.get("Referer") or headers.get("referer") or page_url)
            if parts.scheme and parts.netloc:
                headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
        session = ctx.new_session(headers)
        await session.attach_browser(browser, authority=True)
        session.start_browser_sync(SYNC_INTERVAL)
        return session
