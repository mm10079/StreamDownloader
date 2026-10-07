"""以 curl 子程序下載（Windows 10 以上內建 curl.exe）。
適合需要 curl 特有 TLS 行為的網站；之後可替換成 curl-impersonate。"""
import asyncio
import os
import shutil

from ..session import Session
from .base import Fetcher, FetchRequest, FetchResult


class CurlFetcher(Fetcher):
    name = "curl"
    capabilities = frozenset({"range"})

    def __init__(self, *args, binary: str = "curl", **kwargs):
        super().__init__(*args, **kwargs)
        self.binary = shutil.which(binary) or binary

    async def _fetch(self, req: FetchRequest, session: Session) -> FetchResult:
        part = self.part_path(req.dest)
        cmd = [self.binary, "-sS", "-L", "-o", str(part), "-w", "%{http_code}", "--max-time", str(int(session.timeout * 4))]
        for k, v in (session.headers_for(req.url) | req.headers).items():
            cmd += ["-H", f"{k}: {v}"]
        if session.proxy:
            cmd += ["--proxy", session.proxy]
        if req.byte_range:
            cmd += ["-r", f"{req.byte_range[0]}-{req.byte_range[1]}"]
        cmd.append(req.url)

        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        code = out.decode().strip()
        status = int(code) if code.isdigit() else None
        if proc.returncode != 0 or status not in (200, 206):
            part.unlink(missing_ok=True)
            return FetchResult(ok=False, status=status, error=err.decode(errors="ignore").strip() or f"HTTP {status}")
        size = part.stat().st_size
        os.replace(part, req.dest)
        return FetchResult(ok=True, status=status, size=size)
