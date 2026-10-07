import os

from ..session import Session
from .base import Fetcher, FetchRequest, FetchResult

CHUNK = 256 * 1024


class HttpxFetcher(Fetcher):
    """預設 Fetcher：直接使用 Session 的 httpx client，cookies 自動雙向同步"""
    name = "httpx"
    capabilities = frozenset({"range"})

    async def _fetch(self, req: FetchRequest, session: Session) -> FetchResult:
        headers = dict(req.headers)
        if req.byte_range:
            headers["Range"] = f"bytes={req.byte_range[0]}-{req.byte_range[1]}"
        part = self.part_path(req.dest)

        async with session.client().stream("GET", req.url, headers=headers) as resp:
            if resp.status_code not in (200, 206):
                return FetchResult(ok=False, status=resp.status_code, error=f"HTTP {resp.status_code}")
            expected = int(resp.headers.get("Content-Length", 0) or 0)
            size = 0
            with open(part, "wb") as f:
                async for chunk in resp.aiter_bytes(CHUNK):
                    f.write(chunk)
                    size += len(chunk)

        # 有 Content-Encoding 時長度以壓縮後計算，無法比對
        if expected and size != expected and "content-encoding" not in resp.headers:
            part.unlink(missing_ok=True)
            return FetchResult(ok=False, status=resp.status_code, error=f"大小不符 {size}/{expected}")
        if size == 0:
            part.unlink(missing_ok=True)
            return FetchResult(ok=False, status=resp.status_code, error="空檔案")
        os.replace(part, req.dest)      # 寫完才改名 → 存在的檔案必定完整
        return FetchResult(ok=True, status=resp.status_code, size=size)
