import asyncio
import hashlib
from datetime import datetime
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
from ...postprocess.cenc import init_kids, pick_cenc_key
from .decrypt import check_aes128, decrypt_aes128, resolve_iv

AUDIO_EXTS = {"aac", "m4a", "mp3"}
SAVE_EVERY = 20


class HlsProtocol(StreamProtocol):
    kind = StreamKind.HLS

    def __init__(self, ctx: StreamContext, track: str = "main"):
        """track：main 為畫質（影像，或已含音訊）；audio 為主播放清單 EXT-X-MEDIA 指定的獨立音訊軌，由 main 建立並同時下載。
        有獨立音訊軌時，影像存在 backup/{title}/video/、音訊存在 backup/{title}/audio/；否則直接存在 backup/{title}/"""
        super().__init__(ctx)
        self.track = track
        self.paths = StreamPaths(ctx.output_dir, ctx.stream.title, sub="audio" if track == "audio" else "")
        self.name = self.paths.title + (" [audio]" if track == "audio" else "")
        self.store: SegmentStore
        self.master_url: Optional[str] = None
        self.media_url = ctx.stream.url
        self.template: Optional[UrlTemplate] = None
        self.backfill_enabled = False
        self._rebase: Callable[[parser.HlsSegment], str] = lambda seg: seg.uri
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_bytes: dict[str, bytes] = {}
        self._user_keys = self._parse_user_keys(ctx.options.key_list)
        self._tg: Optional[asyncio.TaskGroup] = None
        self._progress: Optional[log.Progress] = None
        self._since_save = 0
        self.audio_url: Optional[str] = None            # main：選定畫質的獨立音訊軌網址
        self.audio: Optional["HlsProtocol"] = None
        self._audio_result: Optional[StreamResult] = None

    # ======================================================================
    # 主流程
    # ======================================================================
    async def run(self) -> StreamResult:
        opts = self.ctx.options
        playlist = await self._load_first_playlist()        # 決定是否有獨立音訊軌，影響資料夾位置
        self.paths.ensure(decrypted=opts.decrypt)
        self.store = SegmentStore.load(self.paths.store)
        await self._setup_keying(playlist)
        self._ingest(playlist)
        if self.track == "main" and self.audio_url:
            self.audio = HlsProtocol(self.ctx, track="audio")

        if len(self._user_keys) > 1:
            await self._verify_user_keys()

        self._progress = await log.Progress.create(self.paths.title, total=len(self.store))
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                if self.audio is not None:
                    tg.create_task(self._run_audio())
                for seg in self.store.pending():
                    self._schedule(seg)
                if self.backfill_enabled and self.template is not None and self.store.data.key_mode == "template":
                    tg.create_task(self._guard(self._backfill(), "回溯"))
                if playlist.is_live:
                    tg.create_task(self._guard(self._monitor(playlist), "直播監控"))
        finally:
            self.store.save()

        return await self._finish()

    async def _run_audio(self) -> None:
        """音訊軌失敗不中斷影像下載，但整體視為未完成（不合併）"""
        try:
            self._audio_result = await self.audio.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = str(e) if isinstance(e, StreamError) else f"{type(e).__name__}: {e}"
            await log.error(f"[{self.audio.name}] 音訊軌下載失敗：{msg}")
            self._audio_result = StreamResult(complete=False, failed=[f"音訊軌：{msg}"])

    async def _guard(self, coro, name: str) -> None:
        """背景工作失敗只記錄，不中斷其他片段的下載"""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await log.error(f"[{self.name}] {name}失敗：{type(e).__name__}: {e}")

    # ======================================================================
    # 播放清單
    # ======================================================================
    async def _load_first_playlist(self) -> parser.MediaPlaylist:
        text, status = await request_text(self.session, self.ctx.stream.url)
        if text is None:
            raise StreamError(f"無法讀取播放清單 (HTTP {status}): {self.ctx.stream.url}")
        if parser.is_master(text):
            self.master_url = self.ctx.stream.url
            if self.track == "main":
                self._backup(self.master_url, text)     # 主播放清單放在 backup/{title}/playlists
            await self._select_variant(text)
            if self.track == "main" and self.audio_url:
                self.paths = StreamPaths(self.ctx.output_dir, self.ctx.stream.title, sub="video")
            text = None
        playlist = await self._load_media(text)
        if playlist is None or not playlist.segments:
            raise StreamError(f"媒體播放清單沒有片段: {self.media_url}")
        drm = next((s.key for s in playlist.segments if s.key and s.key.drm), None)
        if drm is not None and not self._user_keys:
            raise StreamError(f"此串流受 DRM 保護（{drm.drm_name}），金鑰只提供給授權的解密模組，無法下載解密。"
                              f"若你合法持有金鑰，可用 --key KEY 提供")
        await log.info(f"[{self.name}] 媒體播放清單：{self.media_url}"
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
        audio = master.audio_for(variant)
        if self.track == "audio":
            if audio is None:
                raise StreamError(f"主播放清單中找不到音訊軌（{variant.audio_group}）")
            if audio.uri != self.media_url:
                self.media_url = audio.uri
                others = [r.name or r.language for r in master.renditions
                          if r.type == "AUDIO" and r.group_id == audio.group_id and r.uri and r is not audio]
                await log.info(f"[{self.name}] 選擇音訊軌：{audio.name or audio.language or audio.group_id}"
                               + (f"（其他：{'、'.join(others)}）" if others else ""))
            return True
        if variant.uri != self.media_url:
            self.media_url = variant.uri
            await log.info(f"[{self.name}] 選擇畫質：{variant.name or variant.resolution or variant.bandwidth}")
        self.audio_url = audio.uri if audio else None
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
        self.paths.playlists.mkdir(parents=True, exist_ok=True)
        (self.paths.playlists / name).write_text(text, encoding="utf-8")

    # ======================================================================
    # 片段排序鍵與網址修正
    # ======================================================================
    async def _setup_keying(self, pl: parser.MediaPlaylist) -> None:
        # 1. 確認片段網址可用；不可用時嘗試舊版的路徑啟發式（媒體播放清單與主播放清單的各層路徑）
        #    例：Twitter Space 的片段位於主播放清單路徑下，以媒體播放清單路徑解析會得到 HTTP 400
        first = pl.segments[0]
        candidates = self._segment_candidates(first.raw_uri)
        if len(candidates) > 1 and not await self._probe(candidates[0]):
            for idx, url in enumerate(candidates[1:], start=1):
                if await self._probe(url):
                    await log.info(f"[{self.name}] 片段網址改用替代路徑：{url}")
                    self._rebase = lambda seg, i=idx: self._segment_candidates(seg.raw_uri)[i]
                    break

        # 2. 決定排序鍵：能推出網址模板就用網址數字（可與回溯片段共用），否則用媒體序號
        self.backfill_enabled = self.ctx.stream.backfill and self.ctx.options.backfill
        # 續傳上次以網址數字為鍵的進度時，即使這次關閉回溯也要建立模板（只用於排序鍵，不探測），
        # 否則新片段改用媒體序號為鍵，同一片段會被存成兩份
        resume_template = self.store.data.key_mode == "template" and len(self.store) > 0
        urls = [self._rebase(s) for s in pl.segments[:5]]
        tpl = UrlDiffBackfill().build(urls) if (self.backfill_enabled or resume_template) and len(urls) >= 2 else None
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
            await log.debug(f"[{self.name}] 網址模板：{self.template.pattern}（間距 {self.template.space}）")
        if not self.backfill_enabled:
            removed = self.store.drop_backfilled()
            if removed:
                await log.info(f"[{self.name}] 已關閉回溯：略過上次回溯加入的 {removed} 個片段")

    def _segment_candidates(self, raw_uri: str) -> list[str]:
        """片段可能的完整網址：第一個為標準 urljoin（以媒體播放清單為基準），其後為替代路徑。
        順序只取決於播放清單網址的層數，因此同一條串流中以索引選用是穩定的"""
        candidates = parser.legacy_base_candidates(self.media_url, raw_uri)
        if self.master_url:
            for url in parser.legacy_base_candidates(self.master_url, raw_uri):
                if url not in candidates:
                    candidates.append(url)
        return candidates

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
                await log.warning(f"[{self.name}] 讀取播放清單失敗（{errors}/{opts.live_error_limit}）")
                if errors >= opts.live_error_limit:
                    break
                continue
            errors = 0
            added = self._ingest(pl)
            idle = 0 if added else idle + 1
            await self._update_progress()
            if pl.endlist:
                await log.info(f"[{self.name}] 直播已結束（ENDLIST）")
                break
            if idle >= opts.live_idle_limit:
                await log.info(f"[{self.name}] 連續 {idle} 次沒有新片段，停止監控")
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
        await log.info(f"[{self.name}] 開始回溯較早片段（目前最早序號 {min(known)}）")
        strategy = UrlDiffBackfill(distance=self.ctx.options.backfill_distance)
        nums = await strategy.discover(tpl, known, lambda n: self._probe(tpl.format(n)))
        if not nums:
            await log.info(f"[{self.name}] 沒有找到更早的片段")
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
        await log.success(f"[{self.name}] 回溯新增 {len(nums)} 個片段（{nums[0]} ~ {nums[-1]}）")
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
                seg.encryption.local = await self._ensure_aux(seg.encryption.uri, "key", ".key", samples=[seg])

            if not dest.exists():
                seg.status = SegStatus.RUNNING
                res = await self._fetch(seg, dest)
                if not res.ok:
                    seg.status = SegStatus.FAILED
                    seg.retries += 1
                    await log.error(f"[{self.name}] 片段 {seg.key} 下載失敗：{res.error or res.status}")
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
            await log.error(f"[{self.name}] 片段 {seg.key} 發生錯誤：{e}")
        finally:
            self._since_save += 1
            if self._since_save >= SAVE_EVERY:
                self._since_save = 0
                self.store.save()
            await self._update_progress()

    async def _fetch(self, seg: Segment, dest: Path):
        return await fetch_with_retry(self.ctx.fetcher, FetchRequest(
            url=seg.url, dest=dest, session_id=self.ctx.stream.session_id, byte_range=seg.byte_range,
        ), retries=self.ctx.options.retries)

    @staticmethod
    def _parse_user_keys(keys: list[tuple[str, str]]) -> list[tuple[str, bytes]]:
        """--key 提供的金鑰 [(kid, key)]；多組時由 _pick_user_key 依 KID 對應或試解選出"""
        result = []
        for kid, text in keys:
            try:
                key = bytes.fromhex(text.removeprefix("0x"))
            except ValueError:
                raise StreamError(f"--key 不是有效的 hex：{text}") from None
            if len(key) != 16:
                raise StreamError(f"--key 長度錯誤：金鑰應為 16 bytes（32 個 hex 字元），收到 {len(key)} bytes：{text}")
            result.append((kid, key))
        return result

    async def _verify_user_keys(self) -> None:
        """--key 有多組時，開始下載前先以每個金鑰網址的片段試解選出要用的一組"""
        by_uri: dict[str, list[Segment]] = {}
        # 已下載的片段優先（不必再下載），其次播放清單中的片段，回溯片段最後
        for seg in sorted(self.store.ordered(), key=lambda s: (s.from_backfill, s.status is not SegStatus.DONE)):
            if seg.encryption and seg.encryption.method in ("AES-128", "SAMPLE-AES"):
                by_uri.setdefault(seg.encryption.uri, []).append(seg)
        for uri, segs in by_uri.items():
            local = await self._ensure_aux(uri, "key", ".key", samples=segs[:3])
            for seg in segs:
                seg.encryption.local = local

    async def _pick_user_key(self, url: str, samples: list[Segment]) -> bytes:
        """--key 只有一組時直接使用。多組時：
        1. fMP4：init 的 tenc 標示 KID → 選用 KID 相同的 --key（影像、音訊常是不同 KID）
        2. 以實際片段逐一試解（AES-128 檢查解密結果；SAMPLE-AES fMP4 交給 ffmpeg 試解碼）
        都選不出來時用第一組，照常解密"""
        keys = self._user_keys
        if len(keys) == 1:
            return keys[0][1]
        first = keys[0][1]

        sample = await self._key_sample(samples)
        if sample is None:
            await log.warning(f"[{self.name}] 無法下載用來判斷金鑰的片段，使用 --key 的第一組")
            return first
        seg, data_path = sample

        init_path = self.paths.fragments / seg.init if seg.init else None
        if init_path is not None:
            kids = init_kids(init_path)
            match = next((k for kid, k in keys if kid and kid in kids), None)
            if match is not None:
                await log.info(f"[{self.name}] 金鑰依 KID 對應：{kids[0]} → {match.hex()}")
                return match

        if seg.encryption.method == "AES-128":
            data = data_path.read_bytes()
            results = [check_aes128(data, k, seg.encryption.iv) for _, k in keys]
            idx = next((i for i, r in enumerate(results) if r is True), None)
            if idx is None:
                idx = next((i for i, r in enumerate(results) if r is None), None)
        elif init_path is not None:        # SAMPLE-AES fMP4（cbcs）：與合併時相同，交給 ffmpeg 解密試解碼
            trial = data_path.with_name("keytest_" + seg.filename)
            trial.write_bytes(init_path.read_bytes() + data_path.read_bytes())
            try:
                hexes = [k.hex() for _, k in keys]
                chosen = await pick_cenc_key(trial, hexes, self.ctx.options.ffmpeg, via_ffmpeg=True)
            finally:
                trial.unlink(missing_ok=True)
            idx = hexes.index(chosen) if chosen else None
        else:
            await log.warning(f"[{self.name}] SAMPLE-AES（TS）無法試解判斷金鑰，使用 --key 的第一組")
            return first

        if idx is None:
            await log.warning(f"[{self.name}] 金鑰試解：{len(keys)} 組都沒有解出可辨識的內容，使用第一組（{first.hex()}）")
            return first
        await log.info(f"[{self.name}] 金鑰試解：第 {idx + 1} 組正確（{keys[idx][1].hex()}）")
        return keys[idx][1]

    async def _key_sample(self, samples: list[Segment]) -> Optional[tuple[Segment, Path]]:
        """取得用來判斷金鑰的片段（含 init）；已下載的直接使用，下載的片段之後不會重複下載"""
        for seg in samples:
            if not seg.encryption:
                continue
            dest = self.paths.fragments / seg.filename
            try:
                if seg.init_url and not seg.init:
                    seg.init = await self._ensure_aux(seg.init_url, "init", Path(url_basename(seg.init_url)).suffix or ".mp4")
            except StreamError:
                continue
            if not dest.exists():
                res = await self._fetch(seg, dest)
                if not res.ok:
                    continue
                seg.size = res.size
            return seg, dest
        return None

    async def _ensure_aux(self, url: str, prefix: str, ext: str, samples: Optional[list[Segment]] = None) -> str:
        """下載金鑰 / init 片段；同一網址只下載一次，以網址雜湊命名。
        使用者以 --key 提供金鑰時不下載，以 samples 片段試解選出正確的金鑰後寫入本地金鑰檔
        （供本地播放清單與 ffmpeg 合併使用）"""
        name = f"{prefix}_{hashlib.md5(url.encode()).hexdigest()[:10]}{ext}"
        dest = self.paths.fragments / name
        lock = self._key_locks.setdefault(url, asyncio.Lock())
        async with lock:
            if prefix == "key" and self._user_keys:
                if url not in self._key_bytes:
                    key = await self._pick_user_key(url, samples or [])
                    dest.write_bytes(key)
                    self._key_bytes[url] = key
                return name
            if not dest.exists():
                res = await fetch_with_retry(self.ctx.fetcher, FetchRequest(
                    url=url, dest=dest, session_id=self.ctx.stream.session_id), retries=self.ctx.options.retries)
                if not res.ok:
                    raise StreamError(f"{prefix} 下載失敗：{url}（{res.error or res.status}）")
                if prefix == "key":
                    await log.info(f"[{self.name}] 金鑰：{dest.read_bytes().hex()}")
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
                await self._progress.fail(f"[{self.name}] 沒有成功下載任何片段")
            return StreamResult(complete=False, failed=[s.url for s in failed])

        local = self.paths.fragments / "media.m3u8"
        local.write_text(self._render(done, with_keys=True, prefix=""), encoding="utf-8")
        playlist = local
        methods = {s.encryption.method for s in done if s.encryption}
        if self.ctx.options.decrypt and "SAMPLE-AES" in methods:
            await log.warning(f"[{self.name}] SAMPLE-AES 只加密部分資料，無法逐段解密；合併時由 ffmpeg 解密")
        elif self.ctx.options.decrypt and methods:
            playlist = self.paths.decrypted / "media.m3u8"
            playlist.write_text(self._render(done, with_keys=False, prefix="../fragments/"), encoding="utf-8")

        ext = Path(done[0].filename).suffix.lstrip(".").lower()
        output = self.paths.final("m4a" if ext in AUDIO_EXTS and self.audio is None else "mp4")
        if self._progress:
            msg = f"[{self.name}] 完成 {len(done)} 個片段" + (f"，失敗 {len(failed)} 個" if failed else "")
            await (self._progress.fail(msg) if failed else self._progress.done(msg))
        result = StreamResult(complete=not failed, output=output, playlist=playlist, failed=[s.url for s in failed])
        if self.audio is not None:
            audio = self._audio_result or StreamResult(complete=False, failed=["音訊軌未完成"])
            result.complete = result.complete and audio.complete and audio.playlist is not None
            result.failed += audio.failed
            result.audio_playlist = audio.playlist
            if audio.playlist is not None:
                result.audio_offset = await self._audio_offset(done, [s for s in self.audio.store.ordered()
                                                                     if s.status is SegStatus.DONE])
        return result

    def _seg_duration(self, seg: Segment) -> float:
        return seg.duration or self.store.data.target_duration

    async def _audio_offset(self, video: list[Segment], audio: list[Segment]) -> float:
        """影像與音訊的起點差（秒，音訊較晚為正）。兩軌起點可能不同：直播中途開始下載時播放清單的起點、回溯找到的範圍
        1. 兩軌都有 EXT-X-PROGRAM-DATE-TIME：以時間推算各自第一個片段的開始時間
        2. 否則以兩軌共有的媒體序號為基準，比較該片段在各軌中的位置（不捨棄任何片段）"""
        def start_by_pdt(segs: list[Segment]) -> Optional[float]:
            elapsed = 0.0
            for s in segs:
                if s.program_date_time:
                    try:
                        return datetime.fromisoformat(s.program_date_time).timestamp() - elapsed
                    except ValueError:
                        return None
                elapsed += self._seg_duration(s)
            return None

        def positions(segs: list[Segment]) -> dict[int, float]:
            pos, t = {}, 0.0
            for s in segs:
                if s.media_sequence is not None:
                    pos.setdefault(s.media_sequence, t)
                t += self._seg_duration(s)
            return pos

        v_start, a_start = start_by_pdt(video), start_by_pdt(audio)
        if v_start is not None and a_start is not None:
            offset, how = a_start - v_start, "PROGRAM-DATE-TIME"
        else:
            v_pos, a_pos = positions(video), positions(audio)
            common = next((seq for seq in a_pos if seq in v_pos), None)
            if common is None:
                await log.warning(f"[{self.name}] 影音無法對齊：沒有時間標記，也沒有共同的片段序號；以兩軌起點直接合併")
                return 0.0
            offset, how = v_pos[common] - a_pos[common], f"共同片段序號 {common}"
        if abs(offset) >= 0.0005:
            await log.info(f"[{self.name}] 影音對齊：音訊比影像{'晚' if offset > 0 else '早'} {abs(offset):.3f} 秒開始（依 {how}）")
        return offset

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
            if s.program_date_time:
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{s.program_date_time}")
            lines.append(f"#EXTINF:{s.duration or self.store.data.target_duration:.3f},")
            # 解密版清單中，未加密片段仍放在 fragments 資料夾
            lines.append(s.filename if with_keys or s.encryption else prefix + s.filename)
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"
