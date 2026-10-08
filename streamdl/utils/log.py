"""console.Message 的簡寫，讓業務程式碼只需要 `await log.info(...)`"""
from contextvars import ContextVar
from typing import Optional

from rich.markup import escape

from .console import Message, Color, LogLevel, Status


async def debug(message: str) -> None:
    await Message.log(escape(message), color=Color.GREY, loglevel=LogLevel.DEBUG, status=Status.NONE).push()


async def info(message: str) -> None:
    await Message.log(escape(message), color=Color.WHITE, loglevel=LogLevel.INFO).push()


async def success(message: str) -> None:
    await Message.log(escape(message), color=Color.GREEN, loglevel=LogLevel.INFO, status=Status.COMPLETED).push()


async def warning(message: str) -> None:
    await Message.log(escape(message), color=Color.YELLOW, loglevel=LogLevel.WARNING, status=Status.NONE).push()


async def error(message: str) -> None:
    await Message.log(escape(message), color=Color.RED, loglevel=LogLevel.ERROR, status=Status.ERROR).push()


class Progress:
    """單一進度條的薄包裝"""
    def __init__(self, task_id: int):
        self.task_id = task_id

    @classmethod
    async def create(cls, message: str, total: float = 0) -> "Progress":
        msg = Message.progress(message=escape(message), total=total)
        await msg.push()
        return cls(int(msg.task_id))

    async def update(self, completed: float | None = None, total: float | None = None, message: str | None = None):
        msg = Message.progress_update(task_id=self.task_id, completed=completed,
                                      message=escape(message) if message else None)
        msg.total = total
        await msg.push()

    async def done(self, message: str):
        await Message.progress_complete(task_id=self.task_id, message=escape(message)).push()

    async def fail(self, message: str):
        await Message.progress_error(task_id=self.task_id, message=escape(message)).push()


# ---------------- 互動輸入 ----------------

# None：依 stdin 是否為終端機判斷；True / False：強制（API 預設 False）
INTERACTIVE: ContextVar[Optional[bool]] = ContextVar("streamdl_interactive", default=None)

def interactive() -> bool:
    """是否可以在終端機詢問使用者；API 呼叫時預設關閉（INTERACTIVE），避免卡住呼叫端"""
    import sys
    forced = INTERACTIVE.get()
    if forced is not None:
        return forced
    return bool(sys.stdin) and sys.stdin.isatty()


async def ask(prompt: str, secret: bool = False, default: str = "", stop=None) -> str:
    """在終端機詢問。先暫停 Rich 的進度畫面，否則畫面重繪會蓋掉提示（getpass 在 Windows 直接寫入主控台）。

    - 非互動環境回傳 default
    - stop（asyncio.Event）被設定時（例如 Ctrl+C）立即回傳 default
    - 以 daemon 執行緒讀取輸入：放棄等待時，程式結束不會被卡在尚未完成的 input()
    """
    import asyncio
    import getpass
    import threading
    from .console import RichPusher

    if not interactive():
        return default
    await RichPusher.shutdown()     # 下次輸出 log 時會自動重新啟動
    reader = getpass.getpass if secret else input
    loop = asyncio.get_running_loop()
    answer: asyncio.Future = loop.create_future()

    def worker():
        try:
            result = reader(prompt)
        except (EOFError, OSError):
            result = None       # Windows 上 stdin 導向 NUL 時 isatty() 仍為 True，讀取會直接 EOF
        try:
            loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(result))
        except RuntimeError:
            pass                # 事件迴圈已結束

    threading.Thread(target=worker, daemon=True).start()
    waiters = {answer}
    stop_task = asyncio.ensure_future(stop.wait()) if stop is not None else None
    if stop_task:
        waiters.add(stop_task)
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if stop_task:
            stop_task.cancel()
    if not answer.done() or answer.result() is None:
        print()
        return default
    return answer.result().strip() or default


async def confirm(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = await ask(f"{prompt} {hint} ", default="y" if default else "n")
    return answer.lower().startswith("y")
