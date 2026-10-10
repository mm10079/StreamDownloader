"""主流程：match → extract → 各串流 protocol.run() → postprocess

取代舊的「任務池 + 節點串接」：流程以一般 async 函式表達，
並行由 TaskGroup（串流層級）與 Fetcher 內的 Semaphore（請求層級）控制；
TaskGroup 結束即代表全部完成，不需要毒藥丸 / soft_stop。
"""
import asyncio
import json
import traceback
from pathlib import Path
from typing import Optional

from ..extractor import ExtractContext, LOAD_ERRORS, find_extractor
from ..fetcher import FetchRequest, create_fetcher, fetch_with_retry
from ..postprocess import merge_playlist
from ..protocol import StreamContext, StreamError, StreamResult, protocol_for
from ..session import SessionManager
from ..utils import log
from ..utils.paths import StreamPaths, sanitize_filename
from .models import AttachmentKind, AttachmentSpec, MediaJob, StreamSpec
from .options import Options
from .result import AttachmentOutcome, DownloadResult, StreamOutcome


def _where(e: BaseException) -> str:
    """錯誤發生在本專案內的最後位置（略過第三方套件），方便回報問題"""
    frames = [f for f in traceback.extract_tb(e.__traceback__) if f"{Path(__file__).parents[1]}" in f.filename]
    f = frames[-1] if frames else traceback.extract_tb(e.__traceback__)[-1]
    return f"{Path(f.filename).name}:{f.lineno}"


def _describe(e: BaseException) -> str:
    first = str(e).splitlines()[0] if str(e) else ""
    return f"{type(e).__name__}: {first}（{_where(e)}）"


class Pipeline:
    def __init__(self, options: Options, stop: Optional[asyncio.Event] = None):
        self.options = options
        self.stop = stop or asyncio.Event()
        self.sessions = SessionManager()
        self.fetcher = create_fetcher(options.fetcher, self.sessions, options)
        self.ctx = ExtractContext(options=options, sessions=self.sessions, stop=self.stop)
        self.extractor_name: Optional[str] = None
        self.last_error: Optional[str] = None

    async def run(self, url: str) -> DownloadResult:
        try:
            job = await self.extract(url)
            if job is None:
                return DownloadResult(url=url, extractor=self.extractor_name, error=self.last_error)
            result = await self.execute(job)
            result.url, result.extractor = url, self.extractor_name
            return result
        finally:
            await self.fetcher.aclose()
            await self.sessions.aclose()        # 停止 cookies 同步；瀏覽器本身不受影響
            await self._keep_browsers_open()
            await self.ctx.close()

    async def _keep_browsers_open(self) -> None:
        """有開啟的瀏覽器時，等使用者決定何時關閉（例如下載完成後繼續觀看直播到結束）"""
        if not (self.ctx.browsers and self.options.keep_browser and log.interactive()) or self.stop.is_set():
            return
        await log.info(f"全部任務已結束，{len(self.ctx.browsers)} 個瀏覽器仍保持開啟，可以繼續觀看")
        await log.ask("按 Enter 關閉瀏覽器並結束（或按 Ctrl+C）…", stop=self.stop)

    # ---------------- 解析 ----------------
    async def extract(self, url: str) -> Optional[MediaJob]:
        extractor = find_extractor(url)
        for name, err in LOAD_ERRORS.items():
            await log.debug(f"提取器載入失敗 {name}: {err}")
        if extractor is None:
            self.last_error = f"沒有支援此網址的提取器：{url}"
            await log.error(self.last_error)
            return None
        self.extractor_name = extractor.config.name or type(extractor).__name__
        await log.info(f"使用提取器：{self.extractor_name}")
        try:
            return await extractor.extract(url, self.ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = f"解析失敗：{_describe(e)}"
            await log.error(self.last_error)
            return None

    # ---------------- 執行 ----------------
    async def execute(self, job: MediaJob) -> DownloadResult:
        output_dir = Path(job.output_dir or self.options.output)
        result = DownloadResult(url=self.options.url, title=job.title, output_dir=output_dir)
        if not self.options.media:
            job.streams = []
        if not self.options.attachment:
            job.attachments = []
        if not job.streams and not job.attachments:
            await log.warning("提取結果沒有任何可下載的內容")
            return result

        async with asyncio.TaskGroup() as tg:
            stream_tasks = [tg.create_task(self._run_stream(s, output_dir)) for s in job.streams]
            attach_task = tg.create_task(self._download_attachments(job.attachments, output_dir)) if job.attachments else None
        result.streams = [t.result() for t in stream_tasks]
        if attach_task:
            result.attachments = attach_task.result()
        return result

    async def _run_stream(self, stream: StreamSpec, output_dir: Path) -> StreamOutcome:
        """單一串流失敗不影響其他串流"""
        backup = StreamPaths(output_dir, stream.title).backup
        outcome = StreamOutcome(title=stream.title, kind=stream.kind, url=stream.url, ok=False,
                                backup=backup if backup.exists() else None)
        ctx = StreamContext(stream=stream, options=self.options, sessions=self.sessions,
                            fetcher=self.fetcher, output_dir=output_dir, stop=self.stop)
        try:
            result: StreamResult = await protocol_for(stream.kind)(ctx).run()
        except (StreamError, NotImplementedError) as e:
            outcome.error = str(e)
            await log.error(f"[{stream.title}] {e}")
            return outcome
        except Exception as e:
            outcome.error = f"未預期的錯誤：{_describe(e)}"
            await log.error(f"[{stream.title}] {outcome.error}")
            return outcome
        finally:
            outcome.backup = backup if backup.exists() else None

        outcome.failed = list(result.failed)
        ok = result.complete
        if result.playlist and result.output and self.options.merge:
            if result.complete:
                ok = await merge_playlist(result.playlist, result.output, self.options.ffmpeg,
                                          audio=result.audio_playlist, audio_offset=result.audio_offset)
                if not ok:
                    outcome.error = "合併失敗"
            else:
                await log.warning(f"[{stream.title}] 有 {len(result.failed)} 個片段失敗，未合併。"
                                  f"重新執行相同指令即可續傳；或手動合併：{result.playlist}")
        outcome.ok = ok
        if ok and result.output and result.output.exists():
            outcome.output = result.output
        return outcome

    async def _download_attachments(self, items: list[AttachmentSpec], output_dir: Path) -> list[AttachmentOutcome]:
        async def one(item: AttachmentSpec) -> AttachmentOutcome:
            dest = output_dir / item.path
            dest = dest.with_name(sanitize_filename(dest.name))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item.kind is AttachmentKind.JSON:
                dest.write_text(json.dumps(item.data, ensure_ascii=False, indent=2), encoding="utf-8")
                return AttachmentOutcome(dest, True)
            if item.kind is AttachmentKind.TEXT:
                dest.write_text(str(item.data), encoding="utf-8")
                return AttachmentOutcome(dest, True)
            if dest.exists():
                return AttachmentOutcome(dest, True, item.url)
            res = await fetch_with_retry(self.fetcher, FetchRequest(url=item.url, dest=dest, session_id=item.session_id),
                                         retries=self.options.retries)
            if not res.ok:
                await log.warning(f"附件下載失敗：{item.url}（{res.error or res.status}）")
            return AttachmentOutcome(dest, res.ok, item.url)

        results = await asyncio.gather(*(one(i) for i in items))
        await log.info(f"附件：成功 {sum(r.ok for r in results)} / {len(results)}")
        return list(results)
