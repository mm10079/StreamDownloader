"""DASH 下載

- 影像選擇依 --quality（0 為最高頻寬），音訊取最高頻寬；字幕軌目前略過
- 每軌一個 SegmentStore（backup/{title}/video、audio），可斷點續傳
- 動態 MPD 依 minimumUpdatePeriod 重新讀取，直到轉為 static、連續無新片段或使用者停止
- 回溯：SegmentTemplate 的 media 直接轉成 {num} 模板，不需比對網址推測
    $Number$ → 連續序號，二分搜尋下界
    $Time$   → 以 SegmentTimeline 中出現過的片段長度為間距逐段往前猜
- 完成後每軌以 init + 片段串接成 fMP4，再以 ffmpeg 封裝成單一檔案
"""
import asyncio
import base64
import hashlib
import shutil
from collections import Counter
from pathlib import Path
from typing import Optional

from ...backfill import UrlDiffBackfill, UrlTemplate
from ...core.models import StreamKind
from ...core.store import Segment, SegmentStore, SegStatus
from ...fetcher import FetchRequest, fetch_with_retry, request_text
from ...postprocess import mux_tracks
from ...postprocess.cenc import decrypt_cenc, looks_decodable
from ...postprocess.progress import run_in_thread
from ...utils import log
from ...utils.paths import StreamPaths, url_basename
from ..base import StreamContext, StreamError, StreamProtocol, StreamResult
from .parser import Manifest, MpdError, Representation, parse_mpd

SAVE_EVERY = 20
BACKFILL_MAX_SCAN = 3000    # 間距很大的 $Time$ 不做範圍掃描


class Track:
    """單一軌道（影像或音訊）的下載狀態"""

    def __init__(self, proto: "DashProtocol", kind: str, rep: Representation):
        self.proto = proto
        self.kind = kind
        self.rep_id = rep.id
        self.dir = proto.paths.backup / kind
        self.fragments = self.dir / "fragments"
        self.fragments.mkdir(parents=True, exist_ok=True)
        self.store = SegmentStore.load(self.dir / "store.json")
        self.init_url = rep.init_url
        self.init_range = rep.init_range
        self.init_file: Optional[Path] = None
        self.key_kind = rep.key_kind
        self.time_origin = rep.time_origin
        self.template: Optional[UrlTemplate] = None
        self.hints: list[int] = []
        self.progress: Optional[log.Progress] = None
        self.decrypt_key: Optional[str] = None      # CENC 金鑰（hex）
        self.kid: Optional[str] = None
        self._since_save = 0
        self._init_lock = asyncio.Lock()

        if self.store.data.key_mode and self.store.data.key_mode != rep.key_kind and len(self.store):
            raise StreamError(f"{kind} 軌續傳資料的序號類型不同（{self.store.data.key_mode} → {rep.key_kind}），"
                              f"請刪除 {self.dir} 後重新下載")
        self.store.data.key_mode = rep.key_kind
        self.store.data.source_url = proto.ctx.stream.url

        if rep.url_template and rep.key_kind in ("number", "time"):
            space = 1
            if rep.key_kind == "time" and rep.durations:
                space = Counter(rep.durations).most_common(1)[0][0]
                self.hints = sorted(set(rep.durations), key=lambda d: (-rep.durations.count(d), d))
            self.template = rep.url_template.model_copy(update={"space": space})

    @property
    def title(self) -> str:
        return f"{self.proto.paths.title} [{self.kind}]"

    # ---------- 片段 ----------
    def ingest(self, rep: Representation) -> int:
        added = 0
        for s in rep.segments:
            ext = Path(url_basename(s.url)).suffix or ".m4s"
            seg = Segment(key=s.key, url=s.url, filename=f"{s.key:016d}{ext}", duration=s.duration,
                          init_url=rep.init_url, byte_range=s.byte_range)
            if self.store.add(seg):
                added += 1
                self.proto.schedule(self, seg)
        return added

    async def ensure_init(self) -> None:
        if not self.init_url:
            return
        async with self._init_lock:
            if self.init_file is not None:
                return
            ext = Path(url_basename(self.init_url)).suffix or ".mp4"
            dest = self.fragments / f"init_{hashlib.md5(self.init_url.encode()).hexdigest()[:10]}{ext}"
            if not dest.exists():
                res = await fetch_with_retry(self.proto.ctx.fetcher, FetchRequest(
                    url=self.init_url, dest=dest, session_id=self.proto.ctx.stream.session_id,
                    byte_range=self.init_range), retries=self.proto.ctx.options.retries)
                if not res.ok:
                    raise StreamError(f"{self.kind} 軌 init 下載失敗：{res.error or res.status}")
            self.init_file = dest

    async def download(self, seg: Segment) -> None:
        ctx = self.proto.ctx
        dest = self.fragments / seg.filename
        try:
            await self.ensure_init()
            if not dest.exists():
                seg.status = SegStatus.RUNNING
                res = await fetch_with_retry(ctx.fetcher, FetchRequest(
                    url=seg.url, dest=dest, session_id=ctx.stream.session_id, byte_range=seg.byte_range,
                ), retries=ctx.options.retries)
                if not res.ok:
                    seg.status = SegStatus.FAILED
                    seg.retries += 1
                    await log.error(f"[{self.title}] 片段 {seg.key} 下載失敗：{res.error or res.status}")
                    return
                seg.size = res.size
            seg.status = SegStatus.DONE
        except asyncio.CancelledError:
            if seg.status is SegStatus.RUNNING:
                seg.status = SegStatus.PENDING
            raise
        except Exception as e:
            seg.status = SegStatus.FAILED
            await log.error(f"[{self.title}] 片段 {seg.key} 發生錯誤：{e}")
        finally:
            self._since_save += 1
            if self._since_save >= SAVE_EVERY:
                self._since_save = 0
                self.store.save()
            await self.update_progress()

    async def update_progress(self) -> None:
        if self.progress:
            await self.progress.update(completed=self.store.count(SegStatus.DONE), total=len(self.store))

    # ---------- 回溯 ----------
    async def backfill(self) -> None:
        tpl = self.template
        known = [s.key for s in self.store.ordered() if not s.from_backfill]
        if not known or tpl is None:
            return
        await log.info(f"[{self.title}] 開始回溯較早片段（目前最早 {'時間' if self.key_kind == 'time' else '序號'} {min(known)}）")
        strategy = UrlDiffBackfill(distance=self.proto.ctx.options.backfill_distance, max_scan=BACKFILL_MAX_SCAN)
        nums = await strategy.discover(tpl, known, lambda n: self.proto.probe(tpl.format(n)), hints=self.hints)
        # $Time$：串流開頭的片段長度常與其他片段不同，間距猜不到 → 另外探測時間起點
        lowest = min(nums + known)
        if self.key_kind == "time" and lowest > self.time_origin and await self.proto.probe(tpl.format(self.time_origin)):
            nums.append(self.time_origin)
        nums = sorted(n for n in set(nums) if n not in self.store)
        if not nums:
            await log.info(f"[{self.title}] 沒有找到更早的片段")
            return
        for n in nums:
            url = tpl.format(n)
            ext = Path(url_basename(url)).suffix or ".m4s"
            seg = Segment(key=n, url=url, filename=f"{n:016d}{ext}", init_url=self.init_url, from_backfill=True)
            if self.store.add(seg):
                self.proto.schedule(self, seg)
        await log.success(f"[{self.title}] 回溯新增 {len(nums)} 個片段（{nums[0]} ~ {nums[-1]}）")
        await self.update_progress()

    # ---------- 收尾 ----------
    def concat(self, report=lambda n: None) -> Path:
        """init + 所有片段依序串接成單一 fMP4；report(已串接片段數) 供顯示進度"""
        out = self.dir / f"{self.kind}.mp4"
        tmp = out.with_suffix(".part")
        with open(tmp, "wb") as f:
            if self.init_file is not None:
                f.write(self.init_file.read_bytes())
            for i, seg in enumerate(self.store.ordered(), start=1):
                with open(self.fragments / seg.filename, "rb") as src:
                    shutil.copyfileobj(src, f, 1024 * 1024)
                report(i)
        tmp.replace(out)
        return out


class DashProtocol(StreamProtocol):
    kind = StreamKind.DASH

    def __init__(self, ctx: StreamContext):
        super().__init__(ctx)
        self.paths = StreamPaths(ctx.output_dir, ctx.stream.title)
        self.tracks: list[Track] = []
        self._tg: Optional[asyncio.TaskGroup] = None
        self._clearkeys: dict[str, Optional[str]] = {}

    # ======================================================================
    async def run(self) -> StreamResult:
        manifest = await self._load()
        if manifest is None:
            raise StreamError(f"無法讀取 MPD：{self.ctx.stream.url}")
        self.paths.backup.mkdir(parents=True, exist_ok=True)
        self.tracks = await self._select_tracks(manifest)

        allow_backfill = self.ctx.stream.backfill and self.ctx.options.backfill
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                for track in self.tracks:
                    if not allow_backfill and (removed := track.store.drop_backfilled()):
                        await log.info(f"[{track.title}] 已關閉回溯：略過上次回溯加入的 {removed} 個片段")
                    for seg in track.store.pending():       # 先排上次未完成的（續傳）
                        self.schedule(track, seg)
                    track.ingest(manifest.find(track.rep_id, track.kind))   # 新片段在 ingest 內排程
                    track.progress = await log.Progress.create(track.title, total=len(track.store))
                    if allow_backfill and track.template is not None:
                        tg.create_task(self._guard(track.backfill(), f"{track.kind} 軌回溯"))
                if manifest.dynamic:
                    tg.create_task(self._guard(self._monitor(manifest), "直播監控"))
        finally:
            for track in self.tracks:
                track.store.save()
        return await self._finish()

    def schedule(self, track: Track, seg: Segment) -> None:
        if self._tg is not None:
            self._tg.create_task(track.download(seg))

    async def _guard(self, coro, name: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await log.error(f"[{self.paths.title}] {name}失敗：{type(e).__name__}: {e}")

    async def probe(self, url: str) -> bool:
        try:
            async with self.session.client().stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
                return resp.status_code in (200, 206)
        except Exception:
            return False

    # ======================================================================
    async def _load(self) -> Optional[Manifest]:
        text, status = await request_text(self.session, self.ctx.stream.url)
        if text is None:
            return None
        (self.paths.backup / "playlists").mkdir(parents=True, exist_ok=True)
        (self.paths.backup / "playlists" / (url_basename(self.ctx.stream.url) or "manifest.mpd")).write_text(
            text, encoding="utf-8")
        try:
            return parse_mpd(text, self.ctx.stream.url)
        except MpdError as e:
            raise StreamError(str(e)) from e

    async def _select_tracks(self, manifest: Manifest) -> list[Track]:
        videos, audios = manifest.by_type("video"), manifest.by_type("audio")
        chosen: list[tuple[str, Representation]] = []
        if videos:
            chosen.append(("video", videos[min(self.ctx.stream.quality, len(videos) - 1)]))
        if audios:
            chosen.append(("audio", audios[0]))
        if not chosen:
            raise StreamError("MPD 中沒有影像或音訊軌")
        keys = [await self._resolve_key(rep) for _, rep in chosen]     # 先確認金鑰，避免下載後才發現無法解密
        tracks = []
        for (kind, rep), key in zip(chosen, keys):
            track = Track(self, kind, rep)
            track.decrypt_key, track.kid = key, rep.default_kid
            tracks.append(track)
        return tracks

    async def _resolve_key(self, rep: Representation) -> Optional[str]:
        """加密軌的解密金鑰：--key（依 KID 對應）→ ClearKey 授權伺服器；都沒有則無法下載"""
        if not rep.protected:
            return None
        keys = self.ctx.options.key_map
        key = keys.get(rep.default_kid or "") or keys.get("")
        if key:
            return key
        if rep.clearkey_laurl and rep.default_kid:
            if rep.default_kid not in self._clearkeys:     # 影像與音訊常共用同一個 KID
                self._clearkeys[rep.default_kid] = await self._clearkey_license(rep.clearkey_laurl, rep.default_kid)
                if self._clearkeys[rep.default_kid]:
                    await log.info(f"[{self.paths.title}] 已從 ClearKey 授權伺服器取得金鑰（KID {rep.default_kid}）")
            if self._clearkeys[rep.default_kid]:
                return self._clearkeys[rep.default_kid]
        kid = f"（KID {rep.default_kid}）" if rep.default_kid else ""
        drm = [name for name in rep.drm_systems if name != "ClearKey"]
        if drm:
            raise StreamError(f"此串流受 DRM 保護（{'、'.join(drm)}）{kid}：金鑰只提供給授權的解密模組，無法下載解密。"
                              f"若你合法持有金鑰，可用 --key KID:KEY 提供")
        if rep.clearkey_laurl:
            raise StreamError(f"ClearKey 授權伺服器沒有提供金鑰{kid}；可用 --key KID:KEY 提供")
        raise StreamError(f"此串流已加密（CENC）{kid}，但 MPD 未提供取得金鑰的方式；可用 --key KID:KEY 提供")

    async def _clearkey_license(self, laurl: str, kid: str) -> Optional[str]:
        """W3C ClearKey：以 JSON 請求，金鑰以明文 JWK 回傳"""
        def b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        def unb64(text: str) -> bytes:
            return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

        try:
            resp = await self.session.client().post(laurl, json={"kids": [b64(bytes.fromhex(kid))], "type": "temporary"})
            if resp.status_code != 200:
                await log.warning(f"[{self.paths.title}] ClearKey 授權伺服器拒絕請求（HTTP {resp.status_code}）")
                return None
            for k in resp.json().get("keys", []):
                if unb64(k["kid"]).hex() == kid:
                    return unb64(k["k"]).hex()
        except Exception as e:
            await log.warning(f"[{self.paths.title}] ClearKey 授權請求失敗：{type(e).__name__}: {e}")
        return None

    async def _monitor(self, manifest: Manifest) -> None:
        opts = self.ctx.options
        longest = max((s.duration for t in self.tracks
                       for s in (manifest.find(t.rep_id, t.kind).segments or [])), default=4)
        interval = min(max(manifest.minimum_update_period or longest or 4, 1.0), 10.0)
        idle = errors = 0
        while not self.ctx.stop.is_set():
            try:
                await asyncio.wait_for(self.ctx.stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                current = await self._load()
            except StreamError as e:
                current = None
                await log.warning(f"[{self.paths.title}] MPD 解析失敗：{e}")
            if current is None:
                errors += 1
                if errors >= opts.live_error_limit:
                    break
                continue
            errors = 0
            added = 0
            for track in self.tracks:
                rep = current.find(track.rep_id, track.kind)
                if rep is not None:
                    added += track.ingest(rep)
                    await track.update_progress()
            idle = 0 if added else idle + 1
            if not current.dynamic:
                await log.info(f"[{self.paths.title}] 直播已結束（MPD 轉為 static）")
                break
            if idle >= opts.live_idle_limit:
                await log.info(f"[{self.paths.title}] 連續 {idle} 次沒有新片段，停止監控")
                break

    async def _finish(self) -> StreamResult:
        failed: list[str] = []
        for track in self.tracks:
            bad = [s for s in track.store.ordered() if s.status is not SegStatus.DONE]
            failed += [s.url for s in bad]
            msg = f"[{track.title}] 完成 {len(track.store) - len(bad)} 個片段" + (f"，失敗 {len(bad)} 個" if bad else "")
            if track.progress:
                await (track.progress.fail(msg) if bad else track.progress.done(msg))

        audio_only = all(t.kind == "audio" for t in self.tracks)
        output = self.paths.final("m4a" if audio_only else "mp4")
        if failed:
            await log.warning(f"[{self.paths.title}] 有 {len(failed)} 個片段失敗，未合併；重新執行相同指令即可續傳")
            return StreamResult(complete=False, output=output, failed=failed)

        files = []
        for track in self.tracks:
            path = await run_in_thread(f"串接 {track.title}", len(track.store), track.concat)
            if track.decrypt_key:
                plain = path.with_name(f"{track.kind}.decrypted.mp4")
                if not await decrypt_cenc(path, plain, track.decrypt_key, track.kid, self.ctx.options.ffmpeg,
                                          label=track.title):
                    return StreamResult(complete=False, output=output, failed=[f"{track.kind} 解密失敗"])
                if await looks_decodable(plain, self.ctx.options.ffmpeg) is False:
                    await log.warning(f"[{track.title}] 解密後的內容無法正常解碼，金鑰可能錯誤（KID {track.kid or '未知'}）")
                path = plain
            files.append(path)
        if self.ctx.options.merge:
            ok = await mux_tracks(files, output, self.ctx.options.ffmpeg)
            return StreamResult(complete=ok, output=output)
        await log.info(f"[{self.paths.title}] 未合併，各軌檔案：{', '.join(str(f) for f in files)}")
        return StreamResult(complete=True, output=None)
