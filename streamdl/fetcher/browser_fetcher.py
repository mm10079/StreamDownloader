"""在瀏覽器頁面內以 fetch() 下載，完全使用頁面自身的 cookies / 指紋。

速度慢且經過 base64 傳輸，僅作為對 cookies / headers 極度敏感網站的備案。
Session 必須已 attach_browser。
"""
import asyncio
import base64
import os

from ..session import Session
from .base import Fetcher, FetchRequest, FetchResult

_SCRIPT = """
const [url, range, done] = [arguments[0], arguments[1], arguments[arguments.length - 1]];
const headers = range ? {Range: range} : {};
fetch(url, {credentials: 'include', headers})
  .then(async r => {
    if (!r.ok) { done({status: r.status}); return; }
    const buf = new Uint8Array(await r.arrayBuffer());
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    done({status: r.status, data: btoa(bin)});
  })
  .catch(e => done({status: 0, error: String(e)}));
"""


class BrowserFetcher(Fetcher):
    name = "browser"
    capabilities = frozenset({"range"})

    def __init__(self, *args, **kwargs):
        kwargs["per_host"] = min(kwargs.get("per_host", 2), 2)   # 單一頁面不宜過多並行
        super().__init__(*args, **kwargs)

    async def _fetch(self, req: FetchRequest, session: Session) -> FetchResult:
        if session.browser is None:
            return FetchResult(ok=False, error="session 未綁定瀏覽器")
        rng = f"bytes={req.byte_range[0]}-{req.byte_range[1]}" if req.byte_range else None
        res = await asyncio.to_thread(session.browser.execute_async_script, _SCRIPT, req.url, rng)
        status = res.get("status")
        if "data" not in res:
            return FetchResult(ok=False, status=status, error=res.get("error", f"HTTP {status}"))
        data = base64.b64decode(res["data"])
        part = self.part_path(req.dest)
        part.write_bytes(data)
        os.replace(part, req.dest)
        return FetchResult(ok=True, status=status, size=len(data))
