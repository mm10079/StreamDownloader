"""ZAN-LIVE 提取器

每個視角的流程（與舊版相同的安全順序）：
  獨立 Session 登入 → 開啟專屬瀏覽器並帶入登入狀態 → 由瀏覽器進入該視角直播間
  → 從瀏覽器頁面取得串流網址、從瀏覽器讀回 cookies → 才開始下載

原因
1. 不同視角共用 cookies / 登入狀態會互相干擾（同帳號同 session 只能維持一個播放）。
2. 進入或重新整理直播間會改變伺服器端的播放狀態；若下載端用的 session 不是「最後進入直播間」
   的那一份，下載會被踢掉。因此直播間頁面一律只由瀏覽器開啟，httpx 永遠不碰直播間，
   下載期間 cookies 定期由瀏覽器同步（瀏覽器為權威）。
"""
import asyncio
import time
from datetime import datetime
from typing import Optional

from ....core.models import AttachmentSpec, MediaJob, StreamKind, StreamSpec
from ....session import Authority, Session
from ....utils import log
from ....utils.paths import sanitize_filename
from ..._extractor import ExtractContext, ExtractorConfig, InfoExtractor
from . import pages
from .attachments import AttachmentBuilder
from .auth import logged_in, login_browser, login_httpx
from .schema.item import DetailMetas, LiveRoomMetas, TicketDetail
from .urls import DOMAIN_RE, ZanUrls, parse_detail, parse_playroom

ROOM_LOAD_TIMEOUT = 60      # 進入直播間後等待頁面載入（秒）
LIVE_URL_POLL = 30          # 開放後等待串流網址出現的輪詢間隔（秒）
LIVE_URL_TIMEOUT = 60 * 60  # 最多等待一小時
SYNC_INTERVAL = 10          # 下載期間從瀏覽器同步 cookies 的間隔（秒）
RELOAD_COOLDOWN = 60        # 同步後仍 403 時，兩次重新進入直播間的最短間隔（秒）


class ZanError(Exception):
    pass


class ZanLiveExtractor(InfoExtractor):
    config = ExtractorConfig(
        name="zan-live",
        description="ZAN-LIVE 直播 / 存檔，含多視角與附件",
        valid_url_regex=DOMAIN_RE + r"/(?:[A-Za-z-]+/)?live/(?:detail|play)/",
        priority=10,
        backfill=False,                     # 舊版 SKIP_FORMAT_URLS 即排除 zan-live
        need_driver=True,
        need_single_driver_session=True,
        need_auth=True,
    )

    def __init__(self, urls: Optional[ZanUrls] = None):
        self.urls = urls or ZanUrls()

    # ======================================================================
    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        opts = ctx.options
        if opts.browser == "never":
            raise ZanError("ZAN-LIVE 必須由瀏覽器進入直播間以維持下載 session，無法使用 --browser never")
        await self._ask_credentials(ctx)

        # 查詢用 session：讀取票券 API 與 detail 頁（不進入直播間）；之後作為第一個視角的 session
        probe = ctx.new_session(self._base_headers())
        if await login_httpx(probe, self.urls, opts.account, opts.password):
            await log.info("ZAN-LIVE：帳號登入成功")

        opened: dict[tuple[str, str], tuple[Session, str]] = {}
        group_id = parse_detail(url)
        ticket_id = live_id = None
        if group_id is None:
            ids = parse_playroom(url)
            if ids is None:
                raise ZanError(f"無法辨識的 ZAN-LIVE 網址：{url}")
            ticket_id, live_id = ids
            # 直播間網址：直接以該視角的瀏覽器進入，從頁面取得票券群組
            session, html = await self._open_angle(ctx, probe, self.urls.playroom(*ids), "", wait=False,
                                                   need_live=False)
            opened[ids] = (session, html)
            group_id = pages.get_meta(pages.soup_of(html), "ticket-group-id")
            if not group_id:
                raise ZanError("直播間頁面沒有 ticket-group-id（可能沒有購票）")

        tickets_raw: list[dict] = (await probe.client().get(self.urls.tickets_api(group_id))).json().get("result", [])
        detail_soup = pages.soup_of((await probe.client().get(self.urls.detail(group_id))).text)
        detail = pages.import_metas(DetailMetas, detail_soup)
        title = pages.raw_metas(detail_soup).get("og:title") or detail.Title or opts.title

        selected = pages.select_tickets(
            tickets_raw, now=datetime.now().astimezone(), ticket_id=ticket_id, live_id=live_id,
            skip=opts.skip_set, playroom_url=self.urls.playroom,
        )
        await log.info(f"ZAN-LIVE：{title}，共 {len(tickets_raw)} 張票，選中 {len(selected)} 張")
        if not selected:
            await log.warning("ZAN-LIVE：沒有直播中或即將開始的票券")
            return MediaJob(title=title)
        for t in selected:
            await log.info(f"  - {t.name} / {pages.open_time(t):%Y-%m-%d %H:%M}（{'直播中' if t.isLive else '未開始'}）")
        if len(selected) > 1 and not (opts.account and opts.password):
            await log.warning(f"ZAN-LIVE：未提供帳密，需要在 {len(selected)} 個瀏覽器視窗中分別手動登入")

        if not await self._wait_open(selected, opts.wait):
            return MediaJob(title=title)

        # 每個視角：獨立登入 + 專屬瀏覽器進入直播間（依序進行，避免同時登入互相干擾）
        job = MediaJob(title=title, output_dir=opts.output / sanitize_filename(title))
        artists = pages.extract_artists(detail_soup)
        top_images = pages.top_title_images(detail_soup)
        seen_urls: set[str] = set()
        for i, ticket in enumerate(selected):
            key = (str(ticket.id), str(ticket.liveId))
            label = f"視角 {i + 1}/{len(selected)}「{ticket.name}」"
            if key in opened:
                session, html = opened[key]
                html = await self._room_html(session, self.urls.playroom(*key), label, opts.wait, html)
            else:
                base = probe if (i == 0 and not opened) else None
                session, html = await self._open_angle(ctx, base, self.urls.playroom(*key), label, opts.wait)
            stream, attachments = await self._build_ticket(ctx, session, ticket, html, tickets_raw,
                                                           detail, artists, top_images)
            job.streams.append(stream)
            for a in attachments:       # 多視角共用的圖片只下載一次
                if a.url is None or a.url not in seen_urls:
                    seen_urls.add(a.url or "")
                    job.attachments.append(a)
        return job

    # ======================================================================
    # 帳密
    # ======================================================================
    async def _ask_credentials(self, ctx: ExtractContext) -> None:
        """未設定帳密且在互動終端機時詢問一次（密碼不回顯）；帳號直接 Enter 則改為瀏覽器手動登入"""
        opts = ctx.options
        if (opts.account and opts.password) or not log.interactive():
            return
        if not opts.account:
            opts.account = await log.ask("ZAN-LIVE 帳號（直接 Enter 改為在瀏覽器手動登入）：")
        if opts.account and not opts.password:
            opts.password = await log.ask("ZAN-LIVE 密碼（輸入時不會顯示）：", secret=True)

    def _base_headers(self) -> dict[str, str]:
        return {"Origin": self.urls.domain, "Referer": self.urls.domain + "/"}

    # ======================================================================
    # 視角：登入 → 瀏覽器進入直播間 → 同步
    # ======================================================================
    async def _open_angle(
        self, ctx: ExtractContext, session: Optional[Session], playroom: str, label: str, wait: bool,
        need_live: bool = True,
    ) -> tuple[Session, str]:
        opts = ctx.options
        has_creds = bool(opts.account and opts.password)
        session = session or ctx.new_session(self._base_headers())

        browser = await ctx.new_browser(headless=opts.headless and has_creds)   # 手動登入一定要有視窗
        await session.attach_browser(browser, authority=False)                 # UA 以瀏覽器為準

        if not logged_in(session) and has_creds:
            await login_httpx(session, self.urls, opts.account, opts.password)
        if logged_in(session):
            # httpx 登入 → 帶入瀏覽器（舊版 httpx_cookies_to_driver）
            await asyncio.to_thread(browser.goto, self.urls.domain)
            await session.push_to_browser()
        elif not await login_browser(browser, self.urls, opts.account, opts.password, label):
            raise ZanError(f"{label}登入逾時")

        # 由瀏覽器進入直播間；之後瀏覽器是 cookies 的唯一權威
        await asyncio.to_thread(browser.goto, playroom)
        session.authority = Authority.BROWSER
        html = await self._room_html(session, playroom, label, wait, need_live=need_live)
        await session.pull_from_browser(replace=True)
        session.start_browser_sync(SYNC_INTERVAL)
        session.set_refresher(self._make_refresher(playroom, label))
        await log.info(f"ZAN-LIVE：{label}已進入直播間（session {session.id}）")
        return session, html

    async def _room_html(self, session: Session, playroom: str, label: str, wait: bool,
                         html: Optional[str] = None, need_live: bool = True) -> str:
        """從瀏覽器讀取直播間頁面，等待 live-url 出現（不以 httpx 讀取，避免改變播放狀態）"""
        browser = session.browser
        start = time.monotonic()
        announced = False
        while True:
            if html is None:
                html = await asyncio.to_thread(lambda: browser.content)
            current = await asyncio.to_thread(lambda: browser.current_url)
            if "/auth/login" in current:
                raise ZanError(f"{label}被導向登入頁，登入狀態無效")
            raw = pages.raw_metas(pages.soup_of(html))
            if raw.get("live-url") or (not need_live and raw.get("ticket-group-id")):
                return html
            elapsed = time.monotonic() - start
            if elapsed < ROOM_LOAD_TIMEOUT:
                await asyncio.sleep(2)                  # 頁面仍在載入
            elif wait and elapsed < LIVE_URL_TIMEOUT:
                if not announced:
                    await log.info(f"ZAN-LIVE：{label}直播間已開放但串流尚未開始，等待中…")
                    announced = True
                await asyncio.sleep(LIVE_URL_POLL)
                await asyncio.to_thread(browser.goto, playroom)
            else:
                raise ZanError(f"{label}直播間沒有串流網址：{playroom}")
            html = None

    def _make_refresher(self, playroom: str, label: str):
        """下載遇到 401/403：先從瀏覽器同步 cookies；短時間內再次失敗才讓瀏覽器重新進入直播間"""
        last_reload = 0.0
        last_pull = 0.0

        async def refresh(session: Session) -> None:
            nonlocal last_reload, last_pull
            now = time.monotonic()
            if session.browser is None:
                return
            if now - last_pull > RELOAD_COOLDOWN or now - last_reload < RELOAD_COOLDOWN:
                last_pull = now
                await session.pull_from_browser()
                return
            last_reload = now
            await log.warning(f"ZAN-LIVE：{label}同步 cookies 後仍被拒，重新進入直播間")
            await asyncio.to_thread(session.browser.goto, playroom)
            await asyncio.sleep(5)
            await session.pull_from_browser()

        return refresh

    # ======================================================================
    # 等待開播
    # ======================================================================
    async def _wait_open(self, tickets: list[TicketDetail], wait: bool) -> bool:
        if any(t.isLive for t in tickets):
            return True
        target = min(pages.open_time(t) for t in tickets)
        if not wait:
            await log.info(f"ZAN-LIVE：尚未開放（{target:%Y-%m-%d %H:%M}），未啟用 --wait")
            return False
        await log.info(f"ZAN-LIVE：等待開放 {target:%Y-%m-%d %H:%M:%S}")
        while (remain := (target - datetime.now().astimezone()).total_seconds()) > 0:
            await asyncio.sleep(min(remain, 600))
            if remain > 600:
                await log.info(f"ZAN-LIVE：距離開放還有 {int(remain - 600) // 60} 分鐘")
        return True

    # ======================================================================
    # 單張票
    # ======================================================================
    async def _build_ticket(
        self, ctx: ExtractContext, session: Session, ticket: TicketDetail, html: str,
        tickets_raw: list[dict], detail: DetailMetas, artists, top_images,
    ) -> tuple[StreamSpec, list[AttachmentSpec]]:
        opts = ctx.options
        playroom = self.urls.playroom(ticket.id, ticket.liveId)
        session.set_header("Referer", playroom)
        metas = pages.import_metas(LiveRoomMetas, pages.soup_of(html))
        live_name = sanitize_filename(metas.Livename)     # 內含 HTML 實體解碼

        stream = StreamSpec(
            kind=StreamKind.HLS, url=metas.Liveurl, title=live_name, session_id=session.id,
            quality=opts.quality, backfill=self.config.backfill,
            extras={"ticket_id": ticket.id, "live_id": ticket.liveId, "playroom": playroom},
        )

        attachments: list[AttachmentSpec] = []
        if opts.attachment:
            comments: list[str] = []
            if metas.VodCommentManifestUrl:
                try:
                    manifest = (await session.client().get(metas.VodCommentManifestUrl)).json()
                    comments = pages.comment_urls(metas.VodCommentManifestUrl, manifest)
                except Exception as e:
                    await log.warning(f"ZAN-LIVE：留言清單讀取失敗：{e}")
            attachments = AttachmentBuilder(self.urls, live_name, session.id).build(
                metas=metas, cover=detail.Image, top_images=top_images,
                gifts=pages.gifts_of(metas), tickets=tickets_raw, artists=artists, comments=comments,
            )
        return stream, attachments
