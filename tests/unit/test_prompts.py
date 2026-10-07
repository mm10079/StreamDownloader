import asyncio
import builtins
import threading
import time

from src.core.options import Options
from src.core.pipeline import Pipeline
from src.utils import log


def test_ask_returns_default_when_stopped(monkeypatch):
    """輸入仍卡住時收到停止訊號：立即回傳預設值，不等待 input()"""
    release = threading.Event()
    monkeypatch.setattr(log, "interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": release.wait(5) and "late")

    async def main():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        t0 = time.monotonic()
        result = await log.ask("問題：", default="預設", stop=stop)
        return result, time.monotonic() - t0

    result, elapsed = asyncio.run(main())
    release.set()
    assert result == "預設" and elapsed < 2


def test_ask_reads_answer(monkeypatch):
    monkeypatch.setattr(log, "interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "  答案  ")
    assert asyncio.run(log.ask("問題：")) == "答案"


def test_ask_eof_returns_default(monkeypatch):
    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(log, "interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", eof)
    assert asyncio.run(log.ask("問題：", default="d")) == "d"


def keep_open_prompted(monkeypatch, browsers: int, stop: bool = False, **opts) -> bool:
    asked = []

    async def fake_ask(prompt, secret=False, default="", stop=None):
        asked.append(prompt)
        return ""

    monkeypatch.setattr(log, "interactive", lambda: True)
    monkeypatch.setattr(log, "ask", fake_ask)

    async def main():
        pipe = Pipeline(Options(**opts))
        pipe.ctx.browsers.extend(object() for _ in range(browsers))
        if stop:
            pipe.stop.set()
        await pipe._keep_browsers_open()
        await pipe.fetcher.aclose()
    asyncio.run(main())
    return bool(asked)


def test_keep_browser_prompt(monkeypatch):
    assert keep_open_prompted(monkeypatch, browsers=1)
    assert not keep_open_prompted(monkeypatch, browsers=0)                       # 沒有瀏覽器
    assert not keep_open_prompted(monkeypatch, browsers=1, keep_browser=False)   # --no-keep-browser
    assert not keep_open_prompted(monkeypatch, browsers=1, stop=True)            # 已按 Ctrl+C
