"""後處理進度：讓長時間的合併 / 串接 / 解密不會看起來像當機"""
import asyncio
from typing import Callable, TypeVar

from ..utils import log

T = TypeVar("T")
REFRESH = 0.5


def hms(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


async def run_in_thread(label: str, total: float, func: Callable[..., T], *args,
                        fmt: Callable[[float], str] = lambda v: f"{v:.0f}") -> T:
    """在背景執行緒執行 func(*args, report)；func 以 report(目前進度) 回報，這裡定期更新進度條"""
    progress = await log.Progress.create(label, total=max(total, 1))
    state = {"value": 0.0}

    def report(value: float) -> None:
        state["value"] = value

    task = asyncio.ensure_future(asyncio.to_thread(func, *args, report))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=REFRESH)
            await progress.update(completed=state["value"],
                                  message=f"{label} {fmt(state['value'])} / {fmt(total)}")
        result = task.result()
    except BaseException:
        await progress.fail(f"{label} 失敗")
        raise
    await progress.done(f"{label} 完成")
    return result
