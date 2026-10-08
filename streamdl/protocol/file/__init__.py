from pathlib import Path

from ...core.models import StreamKind
from ...fetcher import FetchRequest, fetch_with_retry
from ...utils import log
from ...utils.paths import sanitize_filename, url_basename
from ..base import StreamProtocol, StreamResult


class FileProtocol(StreamProtocol):
    """單一檔案（mp4、mp3 等）直接下載"""
    kind = StreamKind.FILE

    async def run(self) -> StreamResult:
        stream = self.ctx.stream
        ext = Path(url_basename(stream.url)).suffix or ".bin"
        dest = self.ctx.output_dir / f"{sanitize_filename(stream.title)}{ext}"
        if dest.exists():
            await log.info(f"檔案已存在：{dest}")
            return StreamResult(complete=True, output=dest)
        res = await fetch_with_retry(self.ctx.fetcher, FetchRequest(
            url=stream.url, dest=dest, session_id=stream.session_id), retries=self.ctx.options.retries)
        if not res.ok:
            await log.error(f"下載失敗：{stream.url}（{res.error or res.status}）")
            return StreamResult(complete=False, failed=[stream.url])
        await log.success(f"下載完成：{dest}")
        return StreamResult(complete=True, output=dest)


__all__ = ["FileProtocol"]
