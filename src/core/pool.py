import asyncio
from pydantic import BaseModel, PrivateAttr
from typing import Any, Optional, Callable
    
class BaseTask(BaseModel):
    priority: int = 2

    _task_queue: Optional[asyncio.PriorityQueue] = PrivateAttr(default=None)
    _download_queue: Optional[asyncio.PriorityQueue] = PrivateAttr(default=None)
    _progress_callback: Optional[Callable] = PrivateAttr(default=None)

    @property
    def task_queue(self):
        return self._task_queue
    
    @property
    def download_queue(self):
        return self._download_queue

    @property
    def progress_callback(self):
        return self._progress_callback
    
    # 💡 核心魔法：定義「小於」的比較規則
    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, BaseTask):
            return NotImplemented
        # 數字越小，優先級越高。當 Python 在 Queue 裡面排隊時，會自動調用這個方法看誰該排前面
        return self.priority < other.priority
    
    async def run(self):
        raise NotImplementedError("Subclasses must implement the run method.")
    
    class Config:
        # 必須開啟，允許 Pydantic 內部寄生非標準的 Python 物件（如 Queue/Client）
        arbitrary_types_allowed = True

class BaseDownloadTask(BaseTask):
    priority: int = 2
    
    _progress_callback: Optional[Callable] = PrivateAttr(default=None)

    @property
    def progress_callback(self):
        return self._progress_callback
    
    # 💡 核心魔法：定義「小於」的比較規則
    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, BaseTask):
            return NotImplemented
        # 數字越小，優先級越高。當 Python 在 Queue 裡面排隊時，會自動調用這個方法看誰該排前面
        return self.priority < other.priority

    async def run(self):
        raise NotImplementedError("Subclasses must implement the run method.")

class TaskPool:
    max_concurrent_tasks = 10

    def __init__(self, task_queue: asyncio.PriorityQueue[BaseTask], progress_callback: Optional[Callable] = None):
        self.task_queue = task_queue
        self.progress_callback = progress_callback

    async def _worker(self):
        while True:
            task = await self.task_queue.get()
            if task is None:
                break
            task._progress_callback = self.progress_callback
            task._task_queue = self.task_queue
            task._download_queue = self.task_queue
            try:
                await task.run()
            except Exception as e:
                print(f"Error executing task: {e}")
            finally:
                self.task_queue.task_done()

class DownloadPool:
    max_concurrent_downloads = 10

    def __init__(self, download_queue: asyncio.PriorityQueue[BaseDownloadTask], progress_callback: Optional[Callable] = None):
        self.download_queue = download_queue
        self.progress_callback = progress_callback

    async def _worker(self):
        while True:
            download_task = await self.download_queue.get()
            if download_task is None:
                break
            download_task._progress_callback = self.progress_callback
            try:
                await download_task.run()
            except Exception as e:
                print(f"Error executing download task: {e}")
            finally:
                self.download_queue.task_done()