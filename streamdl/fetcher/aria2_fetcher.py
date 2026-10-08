"""透過 aria2 JSON-RPC 下載。需先啟動：aria2c --enable-rpc [--rpc-secret=xxx]

限制：aria2 下載期間伺服器回傳的 Set-Cookie 不會寫回 Session，
適合 cookie 不會在下載中輪替的網站。
"""
import asyncio
import os
import uuid

import httpx

from ..session import Session
from .base import Fetcher, FetchRequest, FetchResult


class Aria2Fetcher(Fetcher):
    name = "aria2"
    capabilities = frozenset({"resume"})

    def __init__(self, *args, rpc_url: str, secret: str = "", poll: float = 0.5, **kwargs):
        super().__init__(*args, **kwargs)
        self.rpc_url = rpc_url
        self.secret = secret
        self.poll = poll
        self._rpc = httpx.AsyncClient(timeout=30)

    async def _call(self, method: str, *params):
        args = [f"token:{self.secret}"] if self.secret else []
        payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": args + list(params)}
        resp = await self._rpc.post(self.rpc_url, json=payload)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", data["error"]))
        return data["result"]

    async def _fetch(self, req: FetchRequest, session: Session) -> FetchResult:
        headers = session.headers_for(req.url) | req.headers
        part = self.part_path(req.dest)
        opts = {
            "dir": str(part.parent.resolve()),
            "out": part.name,
            "header": [f"{k}: {v}" for k, v in headers.items()],
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
        }
        if session.proxy:
            opts["all-proxy"] = session.proxy
        gid = await self._call("aria2.addUri", [req.url], opts)
        try:
            while True:
                st = await self._call("aria2.tellStatus", gid, ["status", "completedLength", "errorCode", "errorMessage"])
                if st["status"] == "complete":
                    os.replace(part, req.dest)
                    return FetchResult(ok=True, status=200, size=int(st["completedLength"]))
                if st["status"] in ("error", "removed"):
                    # aria2 errorCode 22 = HTTP 回應錯誤；訊息中含狀態碼
                    status = 403 if "403" in st.get("errorMessage", "") else (401 if "401" in st.get("errorMessage", "") else None)
                    return FetchResult(ok=False, status=status, error=st.get("errorMessage", ""))
                await asyncio.sleep(self.poll)
        except asyncio.CancelledError:
            try:
                await self._call("aria2.forceRemove", gid)
            except Exception:
                pass
            raise

    async def aclose(self) -> None:
        await self._rpc.aclose()
