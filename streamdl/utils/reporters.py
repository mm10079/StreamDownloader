"""當作套件使用時的輸出方式（CLI 使用 console.RichPusher）

- LoggingPusher：寫入標準 logging（logger 名稱 "streamdl"），由呼叫端決定是否顯示、顯示到哪裡
- HookPusher：把訊息轉成簡單的 dict 交給呼叫端的 progress_hook

progress_hook 收到的事件：
    {"type": "log", "level": "INFO", "message": "..."}
    {"type": "progress", "id": 1, "status": "started" | "running" | "finished" | "error",
     "description": "...", "completed": 12.0, "total": 100.0}
"""
import itertools
import logging
from typing import Any, Callable, Optional

from rich.text import Text

from .console import BasePusher, LogLevel, Message, MessageType, Status

logger = logging.getLogger("streamdl")
# 套件慣例：呼叫端沒有設定 logging 時保持安靜（否則 WARNING 以上會由 logging 的 lastResort 印到 stderr）
logger.addHandler(logging.NullHandler())
ProgressHook = Callable[[dict[str, Any]], None]

_ids = itertools.count(1)


def plain(text: Optional[str]) -> str:
    """去掉 Rich 標記（訊息內容在 log.py 中已跳脫，這裡還原為原文）"""
    return Text.from_markup(text).plain if text else ""


def _level_name(level: LogLevel) -> str:
    return logging.getLevelName(int(level))


class LoggingPusher(BasePusher):
    """訊息 → logging；進度只在開始與結束時以 DEBUG / 對應等級記錄，避免洗版"""
    loglevel = LogLevel.DEBUG       # 過濾交給 logging 本身

    async def push(self, message: Message) -> None:
        if message.msg_type is MessageType.LOG:
            if message.message:
                logger.log(int(message.loglevel), plain(message.message))
            return
        if message.msg_type is MessageType.PROGRESS:
            message.task_id = next(_ids)
            logger.debug("開始：%s", plain(message.message))
        elif message.status is Status.COMPLETED:
            logger.info(plain(message.message))
        elif message.status is Status.ERROR:
            logger.error(plain(message.message))


class HookPusher(BasePusher):
    """訊息 → progress_hook(dict)，同時轉給 inner（預設 LoggingPusher）"""
    loglevel = LogLevel.DEBUG

    def __init__(self, hook: ProgressHook, inner: Optional[BasePusher] = None):
        self.hook = hook
        self.inner = inner or LoggingPusher()
        self._descriptions: dict[int, str] = {}
        self._totals: dict[int, Optional[float]] = {}
        self._completed: dict[int, Optional[float]] = {}

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self.hook(event)
        except Exception:
            logger.exception("progress_hook 發生錯誤（已忽略，不影響下載）")

    async def push(self, message: Message) -> None:
        await self.inner.push(message)      # inner 會為新的進度分配 task_id
        text = plain(message.message)
        if message.msg_type is MessageType.LOG:
            if text:
                self._emit({"type": "log", "level": _level_name(message.loglevel), "message": text})
            return

        tid = int(message.task_id)
        if message.msg_type is MessageType.PROGRESS:
            status = "started"
        elif message.status is Status.COMPLETED:
            status = "finished"
        elif message.status is Status.ERROR:
            status = "error"
        else:
            status = "running"
        if text and status in ("started", "running"):
            self._descriptions[tid] = text
        if message.total is not None:
            self._totals[tid] = message.total
        if message.completed is not None:
            self._completed[tid] = message.completed
        self._emit({
            "type": "progress", "id": tid, "status": status,
            "description": self._descriptions.get(tid, text) if status in ("started", "running") else text,
            "completed": self._completed.get(tid), "total": self._totals.get(tid),
        })
        if status in ("finished", "error"):
            for d in (self._descriptions, self._totals, self._completed):
                d.pop(tid, None)


_DEFAULT: Optional[LoggingPusher] = None


def default_pusher() -> LoggingPusher:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LoggingPusher()
    return _DEFAULT
