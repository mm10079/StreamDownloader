import asyncio
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

LIVE_URL_POLL = 30          # 開演後等待串流網址出現的輪詢間隔（秒）
LIVE_URL_TIMEOUT = 60 * 60  # 最多等待一小時


class ZanError(Exception):
    pass


class ZanLiveExtractor(InfoExtractor):
    config = ExtractorConfig(
        name="zan-live",
        description="ZAN-LIVE 直播 / 存檔，含多視角與附件",
        valid_url_regex=DOMAIN_RE + r"/(?:[A-Za-z-]+/)?live/(?:detail|play)/",
        priority=10,
        backfill=False,         # 舊版 SKIP_FORMAT_URLS 即排除 zan-live
        need_auth=True,
    )

    def __init__(self, urls: Optional[ZanUrls] = None):
        self.urls = urls or ZanUrls()

    # ======================================================================
    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        opts = ctx.options
        main = await self._login_session(ctx, first=True)

        # 1. 取得票券群組與選票
        group_id = parse_detail(url)
        ticket_id = live_id = None
        if group_id is None:
            ids = parse_playroom(url)
            if ids is None:
                raise ZanError(f"無法辨識的 ZAN-LIVE 網址：{url}")
            ticket_id, live_id = ids
            room_html = await self._get(main, self.urls.playroom(ticket_id, live_id))
            group_id = pages.get_meta(pages.soup_of(room_html), "ticket-group-id")
            if not group_id:
                raise ZanError("直播間頁面沒有 ticket-group-id（可能沒有購票或未登入）")

        tickets_raw: list[dict] = (await main.client().get(self.urls.tickets_api(group_id))).json().get("result", [])
        detail_soup = pages.soup_of(await self._get(main, self.urls.detail(group_id)))
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

        if not await self._wait_open(selected, opts.wait):
            return MediaJob(title=title)

        # 2. 每張票一條串流；多視角時各自登入取得獨立 session
        job = MediaJob(title=title, output_dir=opts.output / sanitize_filename(title))
        artists = pages.extract_artists(detail_soup)
        top_images = pages.top_title_images(detail_soup)
        seen_urls: set[str] = set()
        for i, ticket in enumerate(selected):
            session = main if i == 0 else await self._login_session(ctx, first=False, base=main)
            stream, attachments = await self._build_ticket(
                ctx, session, ticket, tickets_raw, detail, artists, top_images)
            job.streams.append(stream)
            for a in attachments:       # 多視角共用的圖片只下載一次
                if a.url is None or a.url not in seen_urls:
                    seen_urls.add(a.url or "")
                    job.attachments.append(a)
        return job

    # ======================================================================
    # 登入 / Session
    # ======================================================================
    async def _login_session(self, ctx: ExtractContext, first: bool, base: Optional[Session] = None) -> Session:
        opts = ctx.options
        session = ctx.new_session({"Origin": self.urls.domain, "Referer": self.urls.domain + "/"})

        if opts.browser != "always" and await login_httpx(session, self.urls, opts.account, opts.password):
            await log.info(f"ZAN-LIVE：登入成功（session {session.id}）")
            return session

        if not first and base is not None and not (opts.account and opts.password):
            # 手動登入無法重複登入：共用登入狀態（同帳號同時觀看可能互踢）
            await log.warning("ZAN-LIVE：未提供帳密，多視角共用同一登入狀態，可能被網站限制同時觀看")
            return ctx.sessions.fork(base.id)

        if opts.browser == "never":
            raise ZanError("登入失敗；請提供帳號密碼（STREAMDL_ACCOUNT / STREAMDL_PASSWORD），或允許使用瀏覽器")

        headless = opts.headless and bool(opts.account and opts.password)   # 手動登入一定要有視窗
        browser = await ctx.new_browser(headless=headless)
        if not await login_browser(session, browser, self.urls, opts.account, opts.password):
            raise ZanError("瀏覽器登入逾時")
        await log.info(f"ZAN-LIVE：瀏覽器登入成功（session {session.id}）")
        return session

    async def _get(self, session: Session, url: str) -> str:
        resp = await session.client().get(url)
        if "/auth/login" in str(resp.url):
            raise ZanError(f"被導向登入頁，登入狀態無效：{url}")
        resp.raise_for_status()
        return resp.text

    async def _refresh(self, session: Session, playroom: str, ctx: ExtractContext) -> None:
        """下載中遇到 401/403：重新載入直播間更新 cookies；登入失效時重新登入"""
        if session.authority is Authority.BROWSER and session.browser is not None:
            await asyncio.to_thread(session.browser.goto, playroom)
            await asyncio.sleep(5)
            await session.pull_from_browser()
            return
        resp = await session.client().get(playroom)
        if "/auth/login" in str(resp.url) or not logged_in(session):
            await log.warning(f"ZAN-LIVE：session {session.id} 登入失效，重新登入")
            if await login_httpx(session, self.urls, ctx.options.account, ctx.options.password):
                await session.client().get(playroom)

    async def _bind_browser(self, ctx: ExtractContext, session: Session, playroom: str) -> None:
        """browser=always：以瀏覽器開啟直播間維持 session（舊版行為），之後 cookies 以瀏覽器為準"""
        if session.browser is None:
            browser = await ctx.new_browser()
            await session.attach_browser(browser, authority=False)
            await asyncio.to_thread(browser.goto, self.urls.domain)
            await session.push_to_browser()
        await asyncio.to_thread(session.browser.goto, playroom)
        session.authority = Authority.BROWSER
        await asyncio.sleep(3)
        await session.pull_from_browser()

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

    async def _live_room(self, session: Session, playroom: str, wait: bool) -> tuple[str, LiveRoomMetas]:
        """讀取直播間；開放後串流網址可能晚一點才出現，輪詢等待"""
        waited = 0
        while True:
            html = await self._get(session, playroom)
            soup = pages.soup_of(html)
            raw = pages.raw_metas(soup)
            if raw.get("live-url") or not wait or waited >= LIVE_URL_TIMEOUT:
                metas = pages.import_metas(LiveRoomMetas, soup) if raw.get("live-name") and raw.get("live-url") else None
                if metas is None:
                    raise ZanError(f"直播間沒有串流網址：{playroom}")
                return html, metas
            if waited == 0:
                await log.info("ZAN-LIVE：直播間已開放但串流尚未開始，等待中…")
            await asyncio.sleep(LIVE_URL_POLL)
            waited += LIVE_URL_POLL

    # ======================================================================
    # 單張票
    # ======================================================================
    async def _build_ticket(
        self, ctx: ExtractContext, session: Session, ticket: TicketDetail, tickets_raw: list[dict],
        detail: DetailMetas, artists, top_images,
    ) -> tuple[StreamSpec, list[AttachmentSpec]]:
        opts = ctx.options
        playroom = self.urls.playroom(ticket.id, ticket.liveId)
        session.set_header("Referer", playroom)
        session.set_refresher(lambda s, p=playroom: self._refresh(s, p, ctx))

        if opts.media and opts.browser == "always":
            await self._bind_browser(ctx, session, playroom)

        _, metas = await self._live_room(session, playroom, opts.wait)
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
