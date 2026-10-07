"""console.Message 的簡寫，讓業務程式碼只需要 `await log.info(...)`"""
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
