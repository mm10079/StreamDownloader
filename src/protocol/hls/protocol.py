import asyncio
import hashlib
import math
from pathlib import Path
from typing import Callable, Optional

from ...backfill import UrlDiffBackfill, UrlTemplate
from ...core.models import StreamKind
from ...core.store import KeyInfo, Segment, SegmentStore, SegStatus
from ...fetcher import FetchRequest, fetch_with_retry, request_text
from ...utils import log
from ...utils.paths import StreamPaths, url_basename
from ..base import StreamContext, StreamError, StreamProtocol, StreamResult
from . import parser
from .decrypt import decrypt_aes128, resolve_iv

AUDIO_EXTS = {"aac", "m4a", "mp3"}
SAVE_EVERY = 20


class HlsProtocol(StreamProtocol):
    kind = StreamKind.HLS

    def __init__(self, ctx: StreamContext):
        super().__init__(ctx)
        self.paths = StreamPaths(ctx.output_dir, ctx.stream.title)
        self.store: SegmentStore
        self.master_url: Optional[str] = None
        self.media_url = ctx.stream.url
        self.template: Optional[UrlTemplate] = None
        self._rebase: Callable[[parser.HlsSegment], str] = lambda seg: seg.uri
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_bytes: dict[str, bytes] = {}
        self._tg: Optional[asyncio.TaskGroup] = None
        self._progress: Optional[log.Progress] = None
        self._since_save = 0

    # ======================================================================
    # 主流程
    # ======================================================================
    async def run(self) -> StreamResult:
        opts = self.ctx.options
        self.paths.ensure(decrypted=opts.decrypt)
        self.store = SegmentStore.load(self.paths.store)

        playlist = await self._load_first_playlist()
        await self._setup_keying(playlist)
        self._ingest(playlist)

        self._progress = await log.Progress.create(self.paths.title, total=len(self.store))
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                for seg in self.store.pending():
                    self._schedule(seg)
                if self.template is not None and self.store.data.key_mode == "template":
                    tg.create_task(self._guard(self._backfill(), "回溯"))
                if playlist.is_live:
                    tg.create_task(self._guard(self._monitor(playlist), "直播監控"))
        finally:
            self.store.save()

        return await self._finish()

    async def _guard(self, coro, name: str) -> None:
        """背景工作失敗只記錄，不中斷其他片段的下載"""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await log.error(f"[{self.paths.title}] {name}失敗：{type(e).__name__}: {e}")

    # ======================================================================
    # 播放清單
    # ======================================================================
    async def _load_first_playlist(self) -> parser.MediaPlaylist:
        text, status = await request_text(self.session, self.ctx.stream.url)
        if text is None:
            raise StreamError(f"無法讀取播放清單 (HTTP {status}): {self.ctx.stream.url}")
        if parser.is_master(text):
            self.master_url = self.ctx.stream.url
            self._backup(self.master_url, text)
            await self._select_variant(text)
            text = None
        playlist = await self._load_media(text)
        if playlist is None or not playlist.segments:
            raise StreamError(f"媒體播放清單沒有片段: {self.media_url}")
        drm = next((s.key for s in playlist.segments if s.key and s.key.drm), None)
        if drm is not None:
            raise StreamError(f"此串流受 DRM 保護（{drm.drm_name}），金鑰只提供給授權的解密模組，無法下載解密")
        await log.info(f"[{self.paths.title}] 媒體播放清單：{self.media_url}"
                       f"（{'直播' if playlist.is_live else 'VOD'}，{len(playlist.segments)} 個片段）")
        return playlist

    async def _select_variant(self, text: Optional[str] = None) -> bool:
        if text is None:
            text, _ = await request_text(self.session, self.master_url)
            if text is None:
                return False
        master = parser.parse_master(text, self.master_url)
        if not master.variants:
            raise StreamError("主播放清單沒有任何畫質")
        variant = master.variants[min(self.ctx.stream.quality, len(master.variants) - 1)]
        if variant.uri != self.media_url:
            self.media_url = variant.uri
            await log.info(f"[{self.paths.title}] 選擇畫質：{variant.name or variant.resolution or variant.bandwidth}")
        if variant.audio_group and any(r.uri for r in master.renditions if r.group_id == variant.audio_group):
            await log.warning(f"[{self.paths.title}] 音訊為獨立軌道（{variant.audio_group}），目前僅下載影像軌")
        return True

    async def _load_media(self, text: Optional[str] = None) -> Optional[parser.MediaPlaylist]:
        if text is None:
            text, status = await request_text(self.session, self.media_url)
            if text is None and self.master_url and await self._select_variant():
                text, status = await request_text(self.session, self.media_url)   # 媒體網址可能過期
            if text is None:
                return None
        self._backup(self.media_url, text)
        return parser.parse_media(text, self.media_url)

    def _backup(self, url: str, text: str) -> None:
        name = url_basename(url) or "playlist.m3u8"
        (self.paths.playlists / name).write_text(text, encoding="utf-8")

    # ======================================================================
    # 片段排序鍵與網址修正
    # ======================================================================
    async def _setup_keying(self, pl: parser.MediaPlaylist) -> None:
        # 1. 確認片段網址可用；不可用時嘗試舊版的路徑啟發式
        first = pl.segments[0]
        candidates = parser.legacy_base_candidates(pl.url, first.raw_uri)
        if len(candidates) > 1 and not await self._probe(candidates[0]):
            for idx, url in enumerate(candidates[1:], start=1):
                if await self._probe(url):
                    await log.warning(f"[{self.paths.title}] 片段網址改用替代路徑：{url}")
                    self._rebase = lambda seg, i=idx: parser.legacy_base_candidates(self.media_url, seg.raw_uri)[i]
                    break

        # 2. 決定排序鍵：能推出網址模板就用網址數字（可與回溯片段共用），否則用媒體序號
        stream_allows = self.ctx.stream.backfill and self.ctx.options.backfill
        urls = [self._rebase(s) for s in pl.segments[:5]]
        tpl = UrlDiffBackfill().build(urls) if stream_allows and len(urls) >= 2 else None
        if tpl and all(tpl.extract(self._rebase(s)) is not None for s in pl.segments):
            self.template = tpl
        mode = "template" if self.template else "sequence"
        if self.store.data.key_mode and self.store.data.key_mode != mode and len(self.store):
            # 續傳時沿用上次的鍵，避免同一片段被存成兩份
            mode = self.store.data.key_mode
            if mode == "sequence":
                self.template = None
        self.store.data.key_mode = mode
        self.store.data.source_url = self.ctx.stream.url
        self.store.data.header_lines = pl.header_lines
        if self.template:
            await log.debug(f"[{self.paths.title}] 網址模板：{self.template.pattern}（間距 {self.template.space}）")

    def _key_of(self, seg: parser.HlsSegment, url: str) -> int:
        if self.store.data.key_mode == "template" and self.template:
            num = self.template.extract(url)
            if num is not None:
                return num
        return seg.media_sequence

    # ======================================================================
    # 將播放清單併入 store
    # ======================================================================
    def _ingest(self, pl: parser.MediaPlaylist) -> int:
        self.store.data.target_duration = max(self.store.data.target_duration, pl.target_duration)
        added = 0
        for s in pl.segments:
            url = self._rebase(s)
            key = self._key_of(s, url)
            enc = None
            if s.key:
                enc = KeyInfo(method=s.key.method, uri=s.key.uri,
                              iv=resolve_iv(s.key.iv, s.media_sequence), iv_explicit=bool(s.key.iv))
            seg = Segment(
                key=key, url=url, filename=self._filename(key, url),
                duration=s.duration, media_sequence=s.media_sequence,
                encryption=enc, init_url=s.map_uri,
                byte_range=s.byte_range, program_date_time=s.program_date_time,
                discontinuity=s.discontinuity,
            )
            if self.store.add(seg):
                added += 1
                if self._tg is not None:
                    self._schedule(seg)
        return added

    @staticmethod
    def _filename(key: int, url: str) -> str:
        ext = Path(url_basename(url)).suffix or ".ts"
        return f"{key:010d}{ext}"

    # ======================================================================
    # 直播監控
    # ======================================================================
    async def _monitor(self, playlist: parser.MediaPlaylist) -> None:
        opts = self.ctx.options
        idle = errors = 0
        interval = max(1.0, (playlist.target_duration or 6) / 2)
        while not self.ctx.stop.is_set():
            try:
                await asyncio.wait_for(self.ctx.stop.wait(), timeout=interval)
                break                               # 收到停止訊號
            except asyncio.TimeoutError:
                pass
            pl = await self._load_media()
            if pl is None:
                errors += 1
                await log.warning(f"[{self.paths.title}] 讀取播放清單失敗（{errors}/{opts.live_error_limit}）")
                if errors >= opts.live_error_limit:
                    break
                continue
            errors = 0
            added = self._ingest(pl)
            idle = 0 if added else idle + 1
            await self._update_progress()
            if pl.endlist:
                await log.info(f"[{self.paths.title}] 直播已結束（ENDLIST）")
                break
            if idle >= opts.live_idle_limit:
                await log.info(f"[{self.paths.title}] 連續 {idle} 次沒有新片段，停止監控")
                break

    # ======================================================================
    # 回溯
    # ======================================================================
    async def _probe(self, url: str) -> bool:
        try:
            async with self.session.client().stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
                return resp.status_code in (200, 206)
        except Exception:
            return False

    async def _backfill(self) -> None:
        tpl = self.template
        known = [s.key for s in self.store.ordered() if not s.from_backfill]
        if not known:
            return
        await log.info(f"[{self.paths.title}] 開始回溯較早片段（目前最早序號 {min(known)}）")
        strategy = UrlDiffBackfill(distance=self.ctx.options.backfill_distance)
        nums = await strategy.discover(tpl, known, lambda n: self._probe(tpl.format(n)))
        if not nums:
            await log.info(f"[{self.paths.title}] 沒有找到更早的片段")
            return

        ref = self.store.get(min(known))
        for n in nums:
            url = tpl.format(n)
            enc = None
            est_seq = None
            if ref.media_sequence is not None:
                est_seq = ref.media_sequence - round((ref.key - n) / tpl.space)
            if ref.encryption:
                enc = ref.encryption.model_copy()
                if not enc.iv_explicit:
                    enc.iv = resolve_iv(None, est_seq)
            seg = Segment(key=n, url=url, filename=self._filename(n, url),
                          duration=self.store.data.target_duration, media_sequence=est_seq,
                          encryption=enc, init_url=ref.init_url, from_backfill=True)
            if self.store.add(seg):
                self._schedule(seg)
        await log.success(f"[{self.paths.title}] 回溯新增 {len(nums)} 個片段（{nums[0]} ~ {nums[-1]}）")
        await self._update_progress()

    # ======================================================================
    # 下載
    # ======================================================================
    def _schedule(self, seg: Segment) -> None:
        assert self._tg is not None
        self._tg.create_task(self._download(seg))

    async def _download(self, seg: Segment) -> None:
        opts = self.ctx.options
        dest = self.paths.fragments / seg.filename
        try:
            if seg.init_url:
                seg.init = await self._ensure_aux(seg.init_url, "init", Path(url_basename(seg.init_url)).suffix or ".mp4")
            if seg.encryption and seg.encryption.method in ("AES-128", "SAMPLE-AES"):
                seg.encryption.local = await self._ensure_aux(seg.encryption.uri, "key", ".key")

            if not dest.exists():
                seg.status = SegStatus.RUNNING
                res = await fetch_with_retry(self.ctx.fetcher, FetchRequest(
                    url=seg.url, dest=dest, session_id=self.ctx.stream.session_id, byte_range=seg.byte_range,
                ), retries=opts.retries)
                if not res.ok:
                    seg.status = SegStatus.FAILED
                    seg.retries += 1
                    await log.error(f"[{self.paths.title}] 片段 {seg.key} 下載失敗：{res.error or res.status}")
                    return
                seg.size = res.size
            seg.status = SegStatus.DONE

            if opts.decrypt and seg.encryption and seg.encryption.method == "AES-128":
                key = self._key_bytes.get(seg.encryption.uri) or (self.paths.fragments / seg.encryption.local).read_bytes()
                await asyncio.to_thread(decrypt_aes128, dest, self.paths.decrypted / seg.filename, key, seg.encryption.iv)
        except asyncio.CancelledError:
            if seg.status is SegStatus.RUNNING:
                seg.status = SegStatus.PENDING
            raise
        except Exception as e:
            seg.status = SegStatus.FAILED
            await log.error(f"[{self.paths.title}] 片段 {seg.key} 發生錯誤：{e}")
        finally:
            self._since_save += 1
            if self._since_save >= SAVE_EVERY:
                self._since_save = 0
                self.store.save()
            await self._update_progress()

    async def _ensure_aux(self, url: str, prefix: str, ext: str) -> str:
        """下載金鑰 / init 片段；同一網址只下載一次，以網址雜湊命名"""
        name = f"{prefix}_{hashlib.md5(url.encode()).hexdigest()[:10]}{ext}"
        dest = self.paths.fragments / name
        lock = self._key_locks.setdefault(url, asyncio.Lock())
        async with lock:
            if not dest.exists():
                res = await fetch_with_retry(self.ctx.fetcher, FetchRequest(
                    url=url, dest=dest, session_id=self.ctx.stream.session_id), retries=self.ctx.options.retries)
                if not res.ok:
                    raise StreamError(f"{prefix} 下載失敗：{url}（{res.error or res.status}）")
                if prefix == "key":
                    await log.info(f"[{self.paths.title}] 金鑰：{dest.read_bytes().hex()}")
            if prefix == "key":
                self._key_bytes[url] = dest.read_bytes()
        return name

    async def _update_progress(self) -> None:
        if self._progress:
            await self._progress.update(completed=self.store.count(SegStatus.DONE), total=len(self.store))

    # ======================================================================
    # 收尾：產生本地播放清單
    # ======================================================================
    async def _finish(self) -> StreamResult:
        failed = [s for s in self.store.ordered() if s.status is not SegStatus.DONE]
        done = [s for s in self.store.ordered() if s.status is SegStatus.DONE]
        if not done:
            if self._progress:
                await self._progress.fail(f"[{self.paths.title}] 沒有成功下載任何片段")
            return StreamResult(complete=False, failed=[s.url for s in failed])

        local = self.paths.fragments / "media.m3u8"
        local.write_text(self._render(done, with_keys=True, prefix=""), encoding="utf-8")
        playlist = local
        methods = {s.encryption.method for s in done if s.encryption}
        if self.ctx.options.decrypt and "SAMPLE-AES" in methods:
            await log.warning(f"[{self.paths.title}] SAMPLE-AES 只加密部分資料，無法逐段解密；合併時由 ffmpeg 解密")
        elif self.ctx.options.decrypt and methods:
            playlist = self.paths.decrypted / "media.m3u8"
            playlist.write_text(self._render(done, with_keys=False, prefix="../fragments/"), encoding="utf-8")

        ext = Path(done[0].filename).suffix.lstrip(".").lower()
        output = self.paths.final("m4a" if ext in AUDIO_EXTS else "mp4")
        if self._progress:
            msg = f"[{self.paths.title}] 完成 {len(done)} 個片段" + (f"，失敗 {len(failed)} 個" if failed else "")
            await (self._progress.fail(msg) if failed else self._progress.done(msg))
        return StreamResult(complete=not failed, output=output, playlist=playlist, failed=[s.url for s in failed])

    def _render(self, segs: list[Segment], with_keys: bool, prefix: str) -> str:
        """重建本地播放清單。加密時每段寫出明確 IV，避免本地 MEDIA-SEQUENCE 從 0 開始導致 IV 錯誤"""
        target = math.ceil(max([self.store.data.target_duration] + [s.duration for s in segs]))
        lines = ["#EXTM3U", *self.store.data.header_lines,
                 f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
        last_init = last_key = None
        for s in segs:
            if s.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            if s.init and s.init != last_init:
                lines.append(f'#EXT-X-MAP:URI="{prefix}{s.init}"')
                last_init = s.init
            if with_keys:
                key = (s.encryption.method, s.encryption.local, s.encryption.iv) if s.encryption else None
                if key != last_key:
                    lines.append(f'#EXT-X-KEY:METHOD={key[0]},URI="{key[1]}",IV=0x{key[2]}' if key
                                 else "#EXT-X-KEY:METHOD=NONE")
                    last_key = key
            lines.append(f"#EXTINF:{s.duration or self.store.data.target_duration:.3f},")
            # 解密版清單中，未加密片段仍放在 fragments 資料夾
            lines.append(s.filename if with_keys or s.encryption else prefix + s.filename)
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"
