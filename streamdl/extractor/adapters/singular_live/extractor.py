"""SINGULAR LIVE 提取器

流程
1. 讀取活動資訊頁（公開）：data-event（票券 / 內容）、og:title、og:image
2. 開啟瀏覽器（cookie 權威）：確認登入 → 以「已購買票券 API」過濾票券
3. 選出要下載的內容（直播 / 存檔；已結束的直播改下載關聯存檔），尚未開放時等待
4. 每個內容（視角）各用一個瀏覽器：第一個沿用主瀏覽器，其餘開新的瀏覽器（固定設定檔的副本，
   含擴充功能與登入 cookies；仍未登入時再自動登入）→ 同時進入各自的直播間
   由瀏覽器進入直播間 → 被導回資訊頁（日本 IP 限定）時等待使用者開 VPN 後手動進入
   → 讀取 ULIZA 播放器參數 → 取得 master playlist
5. 附件（與 ZAN-LIVE 相同的布局，放在每個內容的資料夾）：
   {內容名稱}/
   ├── images/                 cover、poster、seekpreview_*、slides/（章節截圖）
   └── web info/
       ├── tickets/            uliza_params.json
       └── attachments/        event.json（data-event）
"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ....core.models import AttachmentKind, AttachmentSpec, MediaJob, StreamKind, StreamSpec
from ....session import Session
from ....utils import log
from ....utils.paths import sanitize_filename, url_basename
from ..._extractor import ExtractContext, ExtractorConfig, InfoExtractor
from ..direct import DirectExtractor
from . import pages
from .pages import SingularError, Target
from .urls import DOMAIN_RE, SingularUrls, parse_detail, parse_lang, parse_play

LOGIN_TIMEOUT = 600         # 等待手動登入（秒）
LOGIN_READY_WAIT = 90       # 等待登入按鈕可點擊（Turnstile 驗證通過）的上限（秒）
LOGIN_ATTEMPTS = 3          # 點擊後仍停在登入頁時重試的次數
LOGIN_CLICK_WAIT = 8        # 每次點擊後等待結果（秒）
TURNSTILE_HINT_AFTER = 8    # 等待超過幾秒仍未通過驗證時提示使用者

# 登入頁（React）的按鈕在以下條件都成立前為 disabled：
# 1. hydration 完成（輸入框綁定 React 事件後才填值，太早填會被重新渲染清掉）
# 2. 欄位已填寫（React 受控輸入框需透過原生 setter 設值並觸發 input 事件）
# 3. Cloudflare Turnstile 人機驗證通過 —— 由瀏覽器自然完成或使用者手動勾選，程式不介入、不繞過
# 每次呼叫只檢查一次並立即回傳狀態，等待與提示由 Python 控制：
#   clicked / waiting / no-form / not-hydrated；turnstile 表示頁面上有人機驗證
_FILL_LOGIN = """
const [email, password, done] = [arguments[0], arguments[1], arguments[arguments.length - 1]];
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
const hydrated = el => !!el && Object.keys(el).some(k => k.startsWith('__reactProps') || k.startsWith('__reactFiber'));
const fill = (el, value) => {
  el.focus();
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
};
const emailEl = document.getElementById('login-email');
const passwordEl = document.getElementById('login-password');
const button = document.querySelector('button.login-email-submit') || document.querySelector('form button[type="submit"]');
const turnstile = !!document.querySelector('.login-form-turnstile, .cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
if (!emailEl || !passwordEl) { done({state: 'no-form', turnstile}); return; }
if (!hydrated(emailEl) || !hydrated(passwordEl)) { done({state: 'not-hydrated', turnstile}); return; }
if (emailEl.value !== email) fill(emailEl, email);
if (passwordEl.value !== password) fill(passwordEl, password);
const ready = hydrated(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true';
if (ready && emailEl.value === email && passwordEl.value === password) { button.click(); done({state: 'clicked', turnstile}); return; }
done({state: 'waiting', turnstile});
"""
ROOM_LOAD = 5               # 進入直播間後等待頁面導向的時間（秒）
REMIND_EVERY = 60           # 等待時重複提醒的間隔（秒）
PROBE_EVERY = 3             # 被區域限制時，偵測 VPN 是否已連上的間隔（秒）
RETRY_COOLDOWN = 20         # 偵測到可進入但自動進入後仍被擋時，下次自動進入前的冷卻（秒）

# 以瀏覽器本身的網路（含 VPN 擴充功能的代理）讀取直播間頁面，不換頁：判斷現在是否能進入
_PROBE_ROOM = """
const [url, done] = [arguments[0], arguments[arguments.length - 1]];
fetch(url, {credentials: 'include', cache: 'no-store'})
  .then(async r => done({url: r.url, status: r.status, text: await r.text()}))
  .catch(e => done({error: String(e)}));
"""
SYNC_INTERVAL = 10


class SingularLiveExtractor(InfoExtractor):
    config = ExtractorConfig(
        name="singular-live",
        description="SINGULAR LIVE 直播 / 存檔（ULIZA 播放器）與附件",
        valid_url_regex=DOMAIN_RE + r"/[a-z]{2}(?:-[A-Za-z]+)?/(?:event/detail|play)/",
        site_url_regex=DOMAIN_RE,
        priority=10,
        need_driver=True,
        need_auth=True,
    )

    def __init__(self, urls: Optional[SingularUrls] = None):
        self.base_urls = urls

    # ======================================================================
    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        opts = ctx.options
        if opts.browser == "never":
            raise SingularError("SINGULAR LIVE 需要以瀏覽器登入與進入直播間，無法使用 --browser never")

        detail, play = parse_detail(url), parse_play(url)
        lang = (detail or play)[0] if (detail or play) else "zh"
        urls = self.base_urls or SingularUrls(lang=lang)
        ticket_id = content_id = None

        browser = await ctx.new_browser(headless=False, profile=True)   # 可能需要手動登入或進入直播間
        session = ctx.new_session({"Referer": urls.domain + "/", "Origin": urls.domain})
        await session.attach_browser(browser, authority=True)
        session.start_browser_sync(SYNC_INTERVAL)

        # 1. 活動資訊
        if detail:
            html = await self._open(browser, urls.detail(detail[1]))
        elif play:
            _, ticket_id, content_id = play
            html = await self._open(browser, url)       # 直播間頁面或被導回的資訊頁
        else:
            raise SingularError(f"無法辨識的 SINGULAR LIVE 網址：{url}")
        event = pages.parse_event(html)
        if event is None:
            raise SingularError("頁面中找不到活動資料（data-event），請改用活動資訊頁網址")
        title = pages.event_title(event, html)
        await log.info(f"SINGULAR LIVE：{title}")
        if event.get("viewable_region") == "jp_only":
            await log.info("SINGULAR LIVE：此活動僅限日本 IP 觀看，進入直播間時需要開啟 VPN")

        # 2. 登入與已購買票券
        owned = await self._owned_tickets(session, browser, urls, ctx)

        # 3. 選擇內容
        targets, notes = pages.plan_targets(event, owned, datetime.now().astimezone(),
                                            ticket_id, content_id, opts.skip_set)
        for note in notes:
            await log.info(f"SINGULAR LIVE：{note}")
        job = MediaJob(title=title, output_dir=opts.output / sanitize_filename(title))
        if not targets:
            await log.warning("SINGULAR LIVE：沒有可下載的內容（未購買、尚未建立直播間或存檔尚未提供）")
            job.attachments += self._event_attachments(Layout(Path(".")), event, html, urls, session.id, opts)
            return job
        for t in targets:
            state = "可觀看" if t.available else f"{t.opening:%Y-%m-%d %H:%M} 開放"
            await log.info(f"  - [{t.ticket_type}] {t.label}（{'直播' if t.kind == 'live' else '存檔'}，{state}）")

        targets = await self._wait_available(targets, opts.wait, ctx)

        # 4. 每個視角各用一個瀏覽器，同時進入直播間取得串流
        pairs = [(browser, session)]
        for t in targets[1:]:
            await log.info(f"SINGULAR LIVE：為「{t.label}」開啟另一個瀏覽器")
            pairs.append(await self._extra_browser(urls, ctx))
        windows = [f"視角{i} {t.label}" if len(targets) > 1 else t.label for i, t in enumerate(targets, start=1)]
        for name, (b, _) in zip(windows, pairs):
            try:
                await asyncio.to_thread(b.set_label, name)     # 視窗標題標記，分辨哪個瀏覽器負責哪個直播間
            except Exception as e:
                await log.debug(f"SINGULAR LIVE：瀏覽器標記失敗：{type(e).__name__}: {e}")
        built = await asyncio.gather(*(self._build_target(t, b, s, urls, ctx, w)
                                       for t, (b, s), w in zip(targets, pairs, windows)))
        for stream, attachments in built:
            job.streams.append(stream)
            job.attachments += attachments
            job.attachments += self._event_attachments(Layout(Path(stream.title)), event, html, urls, session.id, opts)
        if job.streams:
            await log.info("SINGULAR LIVE：下載期間請保持瀏覽器開啟，並勿在其他地方開啟同一個直播間")
        return job

    # ======================================================================
    # 瀏覽器 / 登入
    # ======================================================================
    async def prepare_browser(self, url: str, browser, ctx: ExtractContext) -> None:
        """瀏覽器監控中使用者進入官網（任何頁面）時先自動登入，之後瀏覽與交接都維持登入狀態"""
        urls = self.base_urls or SingularUrls(lang=parse_lang(url) or "zh")
        session = ctx.new_session({"Referer": urls.domain + "/", "Origin": urls.domain})
        await session.attach_browser(browser, authority=True)
        if await self._fetch_owned(session, urls) is not None:
            await log.info("SINGULAR LIVE：瀏覽器已是登入狀態")
            return
        if await self._auto_login(session, browser, urls, ctx):
            back = urls.domain + f"/{urls.lang}" if "/login" in url else url
            await asyncio.to_thread(browser.goto, back)        # 回到使用者原本瀏覽的頁面
        else:
            await log.info("SINGULAR LIVE：請在瀏覽器中手動登入後繼續瀏覽")

    async def _extra_browser(self, urls: SingularUrls, ctx: ExtractContext) -> tuple:
        """多視角時的其他瀏覽器：設定檔副本（擴充功能、cookies）→ 確認登入，未登入時自動 / 手動登入"""
        browser = await ctx.new_browser(headless=False, profile=True)
        session = ctx.new_session({"Referer": urls.domain + "/", "Origin": urls.domain})
        await session.attach_browser(browser, authority=True)
        session.start_browser_sync(SYNC_INTERVAL)
        await asyncio.to_thread(browser.goto, urls.domain + f"/{urls.lang}")
        await session.pull_from_browser()
        await self._owned_tickets(session, browser, urls, ctx, quiet=True)
        return browser, session

    @staticmethod
    async def _open(browser, url: str) -> str:
        await asyncio.to_thread(browser.goto, url)
        return await asyncio.to_thread(lambda: browser.content)

    async def _owned_tickets(self, session: Session, browser, urls: SingularUrls,
                             ctx: ExtractContext, quiet: bool = False) -> Optional[set[str]]:
        """以已購買票券 API 確認登入；未登入時先自動登入，失敗或沒有帳密則等待使用者在瀏覽器登入"""
        owned = await self._fetch_owned(session, urls)
        if owned is None and await self._auto_login(session, browser, urls, ctx):
            owned = await self._fetch_owned(session, urls)
        if owned is not None:
            if not quiet:
                await log.info(f"SINGULAR LIVE：已登入，可觀看的票券 {len(owned)} 張")
            return owned

        deadline = time.monotonic() + LOGIN_TIMEOUT
        announced = False
        while not ctx.stop.is_set():
            await session.pull_from_browser()
            owned = await self._fetch_owned(session, urls)
            if owned is not None:
                await log.info(f"SINGULAR LIVE：已登入，可觀看的票券 {len(owned)} 張")
                return owned
            if not announced:
                await asyncio.to_thread(browser.goto, urls.login)
                await log.info(f"SINGULAR LIVE：請在瀏覽器中登入（{LOGIN_TIMEOUT // 60} 分鐘內），登入後會自動繼續")
                announced = True
            if time.monotonic() > deadline:
                raise SingularError("等待登入逾時")
            await asyncio.sleep(3)
        raise SingularError("已停止")

    async def _auto_login(self, session: Session, browser, urls: SingularUrls, ctx: ExtractContext) -> bool:
        opts = ctx.options
        if not (opts.account and opts.password) and log.interactive():
            if not opts.account:
                opts.account = await log.ask("SINGULAR LIVE 帳號（信箱，直接 Enter 改為在瀏覽器手動登入）：")
            if opts.account and not opts.password:
                opts.password = await log.ask("SINGULAR LIVE 密碼（輸入時不會顯示）：", secret=True)
        if not (opts.account and opts.password):
            return False

        await log.info("SINGULAR LIVE：自動登入中…")
        await asyncio.to_thread(browser.goto, urls.login)
        for attempt in range(1, LOGIN_ATTEMPTS + 1):
            if not await self._click_login_when_ready(browser, opts.account, opts.password, ctx):
                return False
            if await self._wait_logged_in(session, browser, urls, LOGIN_CLICK_WAIT):
                await session.pull_from_browser()
                await log.info("SINGULAR LIVE：自動登入成功")
                return True
            current = await asyncio.to_thread(lambda: browser.current_url)
            if "/login" not in current:
                break       # 已離開登入頁仍未登入（例如需要額外驗證），不再重試
            await log.debug(f"SINGULAR LIVE：點擊後仍在登入頁，重試（{attempt}/{LOGIN_ATTEMPTS}）")
        await log.warning("SINGULAR LIVE：自動登入失敗（帳號密碼錯誤或需要額外驗證），改為手動登入")
        return False

    async def _click_login_when_ready(self, browser, account: str, password: str, ctx: ExtractContext) -> bool:
        """填寫表單並等待登入按鈕可點擊（hydration 完成、欄位有效、人機驗證通過）後點擊"""
        start = time.monotonic()
        hinted = False
        status: dict = {}
        while time.monotonic() - start < LOGIN_READY_WAIT and not ctx.stop.is_set():
            try:
                status = await asyncio.to_thread(browser.execute_async_script, _FILL_LOGIN, account, password)
            except Exception as e:
                status = {"state": f"error: {type(e).__name__}"}
            state = status.get("state") if isinstance(status, dict) else status
            if state == "clicked":
                return True
            if state == "no-form" and time.monotonic() - start > 15:
                await log.warning("SINGULAR LIVE：找不到登入表單，改為手動登入")
                return False
            if (not hinted and status.get("turnstile") and state == "waiting"
                    and time.monotonic() - start > TURNSTILE_HINT_AFTER):
                await log.info("SINGULAR LIVE：登入頁有人機驗證（Cloudflare Turnstile），等待驗證完成；"
                               "若瀏覽器中出現勾選框，請手動完成驗證")
                hinted = True
            await asyncio.sleep(1)
        reason = "人機驗證未完成" if status.get("turnstile") else "登入按鈕一直無法點擊"
        await log.warning(f"SINGULAR LIVE：{reason}，改為手動登入")
        return False

    async def _wait_logged_in(self, session: Session, browser, urls: SingularUrls, seconds: float) -> bool:
        """頁首出現 LOGOUT 按鈕，或已購票券 API 可正常讀取"""
        end = time.monotonic() + max(seconds, 0)
        while time.monotonic() < end:
            await asyncio.sleep(1)
            html = await asyncio.to_thread(lambda: browser.content)
            if pages.login_state(html) is True:
                return True
            await session.pull_from_browser()
            if await self._fetch_owned(session, urls) is not None:
                return True
        return False

    @staticmethod
    async def _fetch_owned(session: Session, urls: SingularUrls) -> Optional[set[str]]:
        try:
            resp = await session.client().get(urls.available_tickets(), headers={"Accept": "application/json"})
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                return None
            data = resp.json()
        except Exception:
            return None
        tickets = data.get("tickets") if isinstance(data, dict) else data
        if not isinstance(tickets, list):
            return None
        ids = set()
        for t in tickets:
            if isinstance(t, dict):
                ids.add(str(t.get("id") or (t.get("ticket") or {}).get("id")))
        return ids

    # ======================================================================
    # 等待開放
    # ======================================================================
    async def _wait_available(self, targets: list[Target], wait: bool, ctx: ExtractContext) -> list[Target]:
        ready = [t for t in targets if t.available]
        if ready:
            later = len(targets) - len(ready)
            if later:
                await log.info(f"SINGULAR LIVE：先下載目前可觀看的 {len(ready)} 個內容，另有 {later} 個尚未開放")
            return ready
        target_time = min(t.opening for t in targets if t.opening)
        if not wait:
            raise SingularError(f"內容尚未開放（{target_time:%Y-%m-%d %H:%M}），未啟用 --wait")
        await log.info(f"SINGULAR LIVE：等待開放 {target_time:%Y-%m-%d %H:%M:%S}")
        while (remain := (target_time - datetime.now().astimezone()).total_seconds()) > 0:
            try:
                await asyncio.wait_for(ctx.stop.wait(), timeout=min(remain, 600))
                raise SingularError("已停止等待")
            except asyncio.TimeoutError:
                pass
            if remain > 600:
                await log.info(f"SINGULAR LIVE：距離開放還有 {int(remain - 600) // 60} 分鐘")
        return [t for t in targets if t.opening and t.opening <= target_time]

    # ======================================================================
    # 直播間
    # ======================================================================
    async def _enter_room(self, browser, play_url: str, label: str, ctx: ExtractContext,
                          window: Optional[str] = None) -> None:
        """由瀏覽器進入直播間；被導回資訊頁（區域限制）時每隔幾秒偵測，使用者開啟 VPN 後自動重新進入
        （使用者也可以自行在瀏覽器中進入）"""
        target_path = urlsplit(play_url).path.rstrip("/")
        await asyncio.to_thread(browser.goto, play_url)
        await asyncio.sleep(ROOM_LOAD)
        last_remind = 0.0
        next_try = 0.0
        while not ctx.stop.is_set():
            blocked = await self._room_state(browser, target_path)
            if blocked is None:
                return
            now = time.monotonic()
            if now >= next_try and await self._room_reachable(browser, play_url, target_path):
                await log.info(f"SINGULAR LIVE：「{label}」偵測到可以進入直播間，自動重新進入")
                await asyncio.to_thread(browser.goto, play_url)
                await asyncio.sleep(ROOM_LOAD)
                next_try = time.monotonic() + RETRY_COOLDOWN
                continue
            if now - last_remind > REMIND_EVERY:
                reason = "僅限日本 IP（本活動僅限日本國內播放）" if blocked else "未進入直播間"
                await log.warning(f"SINGULAR LIVE：「{label}」{reason}。請在標題為【{window or label}】的瀏覽器開啟日本 VPN，"
                                  f"連上後會自動進入直播間（也可以手動進入：{play_url}）")
                last_remind = now
            await asyncio.sleep(PROBE_EVERY)
        raise SingularError("已停止")

    @staticmethod
    async def _room_state(browser, target_path: str) -> Optional[bool]:
        """None：已在直播間；True：被區域限制；False：不在直播間（其他原因）"""
        current = await asyncio.to_thread(lambda: browser.current_url)
        html = await asyncio.to_thread(lambda: browser.content)
        blocked = pages.region_blocked(html)
        if urlsplit(current).path.rstrip("/") == target_path and not blocked:
            return None
        return blocked

    @staticmethod
    async def _room_reachable(browser, play_url: str, target_path: str) -> bool:
        """在頁面內讀取直播間網址（走瀏覽器的網路與 VPN）：沒有被導回且沒有區域限制訊息 → 可以進入"""
        try:
            res = await asyncio.to_thread(browser.execute_async_script, _PROBE_ROOM, play_url)
        except Exception:
            return False
        if not isinstance(res, dict) or res.get("error") or res.get("status") != 200:
            return False
        return (urlsplit(str(res.get("url", ""))).path.rstrip("/") == target_path
                and not pages.region_blocked(str(res.get("text", ""))))

    async def _build_target(self, t: Target, browser, session: Session, urls: SingularUrls,
                            ctx: ExtractContext, window: Optional[str] = None) -> tuple[StreamSpec, list[AttachmentSpec]]:
        play_url = urls.play(t.ticket_id, t.content_id)
        await self._enter_room(browser, play_url, t.label, ctx, window)
        await session.pull_from_browser()

        resp = await session.client().get(urls.uliza_params(t.uliza_id), headers={"Referer": play_url})
        if resp.status_code != 200:
            raise SingularError(f"ULIZA 播放器參數讀取失敗（HTTP {resp.status_code}）：{t.uliza_id}")
        params = pages.parse_uliza_params(resp.text)
        info = pages.uliza_info(params)
        name = sanitize_filename(t.label or info.title or t.uliza_id)
        await log.info(f"SINGULAR LIVE：「{name}」串流 {info.video}")

        stream = StreamSpec(
            kind=DirectExtractor.detect_kind(info.video) or StreamKind.HLS, url=info.video, title=name,
            session_id=session.id, quality=ctx.options.quality, backfill=self.config.backfill,
            extras={"ticket_id": t.ticket_id, "content_id": t.content_id, "uliza_id": t.uliza_id, "play": play_url},
        )

        layout = Layout(Path(name))
        attachments = [AttachmentSpec(kind=AttachmentKind.JSON, path=layout.tickets / "uliza_params.json", data=params)]
        if ctx.options.attachment:
            attachments += layout.image(urls, info.poster, "poster", session.id)
            for i, slide in enumerate(info.slides, start=1):
                attachments += layout.image(urls, slide, f"{i:03d}", session.id, layout.slides)
            if info.seek_preview:
                seek = urls.absolute(info.seek_preview)
                attachments += layout.image(urls, seek, f"seekpreview_{Path(url_basename(seek)).stem}", session.id)
        return stream, attachments

    # ======================================================================
    # 活動附件
    # ======================================================================
    @staticmethod
    def _event_attachments(layout: "Layout", event: dict, html: str, urls: SingularUrls, session_id: str,
                           opts) -> list[AttachmentSpec]:
        items = [AttachmentSpec(kind=AttachmentKind.JSON, path=layout.attachments / "event.json", data=event)]
        if opts.attachment:
            items += layout.image(urls, pages.og(html, "image"), "cover", session_id)
        return items


class Layout:
    """附件資料夾（與 ZAN-LIVE 一致）"""

    def __init__(self, root: Path):
        self.root = root
        self.images = root / "images"
        self.slides = self.images / "slides"
        self.tickets = root / "web info" / "tickets"
        self.attachments = root / "web info" / "attachments"

    def image(self, urls: SingularUrls, url: Optional[str], stem: str, session_id: str,
              folder: Optional[Path] = None) -> list[AttachmentSpec]:
        url = urls.absolute(url)
        if not url:
            return []
        ext = Path(url_basename(url)).suffix or ".jpg"      # 封面網址沒有副檔名（/kv）
        return [AttachmentSpec(kind=AttachmentKind.URL, path=(folder or self.images) / f"{stem}{ext}",
                               url=url, session_id=session_id)]
