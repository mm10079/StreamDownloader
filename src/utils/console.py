import os
import inspect
from enum import Enum
from typing import Optional, Union, ClassVar
from pydantic import BaseModel
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn, TaskID,
    MofNCompleteColumn, TimeElapsedColumn, TimeRemainingColumn
)
from abc import ABC, abstractmethod

class MessageType(str, Enum):
    LOG = "log"
    PROGRESS = "progress"
    PROGRESS_UPDATE = "progress_update"

    def __str__(self):
        return self.value

class LogLevel(int, Enum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50

    def __int__(self):
        return int(self.value)

class Status(str, Enum):
    DOWNLOADING = "[Downloading]"
    COMPLETED = "[Completed]"
    PROCESSING = "[Processing]"
    INFO = "[Info]"
    ERROR = "[Error]"
    NONE = ""

    def __str__(self):
        return self.value

class Color(str, Enum):
    # --- 核心狀態與通知 (常用於語意化輸出) ---
    RED = "[red]"                # 鮮紅色：失敗、嚴重錯誤 (Error)、阻斷性異常
    BRIGHT_RED = "[bright_red]"  # 亮紅色：緊急警報、核心崩潰、需要立即注意
    
    GREEN = "[green]"            # 綠色：成功 (Success)、完成、檢查通過 (Pass)
    BRIGHT_GREEN = "[bright_green]" # 亮綠色：全數達標、部署成功、系統就緒
    
    YELLOW = "[yellow]"          # 黃色：警告 (Warning)、潛在風險、非致命性異常
    BRIGHT_YELLOW = "[bright_yellow]" # 亮黃色：執行中 (Running)、注意、提示使用者確認
    
    BLUE = "[blue]"              # 藍色：標題、主要區塊名稱、提示訊息 (Notice)
    BRIGHT_BLUE = "[bright_blue]" # 亮藍色：執行中任務名稱、高亮連結、步驟指示
    
    CYAN = "[cyan]"              # 青色/淺藍：一般訊息 (Info)、數據流、統計數值
    BRIGHT_CYAN = "[bright_cyan]" # 亮青色：進度條文字、剩餘時間 (ETA)、傳輸速度
    
    MAGENTA = "[magenta]"        # 洋紅/紫：調試訊息 (Debug)、內部機制追蹤、特殊高亮
    BRIGHT_MAGENTA = "[bright_magenta]" # 亮洋紫色：重要副標題、第三方套件輸出
    
    # --- 灰階與特殊用途 ---
    WHITE = "[white]"            # 白色：標準文字、一般紀錄內容
    BLACK = "[black]"            # 黑色：極少用 (通常配合背景色)，或特殊隱藏文字
    
    GREY = "[grey37]"            # 暗灰色：次要資訊 (Verbose)、時間戳記 (Timestamp)
    DARK_GREY = "[grey23]"      # 極暗灰：進度條未完成背景、已忽略的 log 項目
    
    # --- 樣式重置 ---
    RESET = "[reset]"            # 重置：清除所有樣式，恢復終端機預設

    def __str__(self):
        return self.value

class BasePusher(ABC):
    formatter: str = "{color}{status}{message}"
    loglevel: LogLevel = LogLevel.INFO

    @abstractmethod
    async def push(self, message: 'Message') -> None:
        print(f"{message.color}{message.message}") if message.message else None

class Message(BaseModel):
    task_id: Union[int, bool] = False
    message: Optional[str] = None
    color: Color = Color.RESET
    completed: Optional[float] = None
    total: Optional[float] = None
    advance: Optional[float] = None
    status: Status = Status.NONE
    loglevel: LogLevel = LogLevel.INFO
    msg_type: MessageType = MessageType.LOG

    # 用於儲存定位資訊的欄位
    calling_file: Optional[str] = None # 呼叫者的檔案名稱
    calling_line: Optional[int] = None # 呼叫者的行號
    calling_func: Optional[str] = None # 呼叫者的函式名稱
    calling_class: Optional[str] = None # 呼叫者的類別名稱（如果有的話）

    _pusher: ClassVar[Optional[BasePusher]] = None

    @classmethod
    def register_pusher(cls, pusher: BasePusher):
        cls._pusher = pusher

    @classmethod
    def log(
        cls,
        message: str,
        color: Color = Color.WHITE,
        loglevel: LogLevel = LogLevel.INFO,
        status: Status = Status.INFO,
    ) -> "Message":
        """創建日誌類型訊息"""
        return cls(
            task_id=False,
            message=message,
            color=color,
            status=status,
            loglevel=loglevel,
            msg_type=MessageType.LOG,
        )

    @classmethod
    def progress(
        cls,
        message: str,
        total: float,
        completed: float = 0,
        color: Color = Color.CYAN,
        status: Status = Status.PROCESSING,
    ) -> "Message":
        """創建進度條類型訊息（新任務）"""
        return cls(
            task_id=True,
            message=message,
            total=total,
            completed=completed,
            color=color,
            status=status,
            msg_type=MessageType.PROGRESS,
        )

    @classmethod
    def progress_update(
        cls,
        task_id: int,
        advance: Optional[float] = None,
        completed: Optional[float] = None,
        message: Optional[str] = None,
        status: Status = Status.PROCESSING,
        color: Color = Color.CYAN,
    ) -> "Message":
        """更新既有進度條"""
        return cls(
            task_id=task_id,
            message=message,
            advance=advance,
            completed=completed,
            status=status,
            color=color,
            msg_type=MessageType.PROGRESS_UPDATE,
        )

    @classmethod
    def progress_complete(
        cls,
        task_id: int,
        message: str = "完成",
        color: Color = Color.GREEN,
    ) -> "Message":
        """標記進度條為完成"""
        return cls(
            task_id=task_id,
            message=message,
            status=Status.COMPLETED,
            color=color,
            msg_type=MessageType.PROGRESS_UPDATE,
        )

    @classmethod
    def progress_error(
        cls,
        task_id: int,
        message: str = "錯誤",
        color: Color = Color.RED,
    ) -> "Message":
        """標記進度條為錯誤"""
        return cls(
            task_id=task_id,
            message=message,
            status=Status.ERROR,
            color=color,
            msg_type=MessageType.PROGRESS_UPDATE,
        )

    def capture_caller_frame(self):
        """💡 在這裡抓取呼叫棧，此時的 f_back 才是真正的業務邏輯層！"""
        try:
            current_frame = inspect.currentframe()
            # frame(0): capture_caller_frame
            # frame(1): Message.push
            # frame(2): 呼叫 msg.push() 的那行外圍業務代碼
            if current_frame and current_frame.f_back and current_frame.f_back.f_back:
                caller_frame = current_frame.f_back.f_back
                frame_info = inspect.getframeinfo(caller_frame)
                
                self.calling_file = os.path.basename(frame_info.filename)
                self.calling_line = frame_info.lineno
                self.calling_func = frame_info.function
                
                if "self" in caller_frame.f_locals:
                    self.calling_class = caller_frame.f_locals["self"].__class__.__name__
                else:
                    self.calling_class = None
        except Exception:
            pass

    async def push(self):
        """將訊息推送給註冊的 Pusher"""
        # 💡 在進入 Pusher 前先抓取位置，確保層級正確
        if self._pusher is not None:
            if self._pusher.loglevel == LogLevel.DEBUG:
                self.capture_caller_frame()
            await self._pusher.push(self)
        else:
            print(f"{self.color}{self.message}")


class RichPusher(BasePusher):
    # 💡 明確定義類別變數，供所有實例或類別方法共用
    _progress: ClassVar[Optional[Progress]] = None

    @classmethod
    def get_progress(cls) -> Progress:
        """惰性點火：第一次需要用 UI 時，自動啟動 Rich Progress"""
        if cls._progress is None:
            cls._progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=None),
                MofNCompleteColumn(),   # 片段數；直播時總數會持續增加
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                disable=False,
            )
            cls._progress.start() 
        return cls._progress

    @classmethod
    async def shutdown(cls):
        """主程式最後關機時呼叫，優雅還原終端機畫面（移除 async，Rich 的 stop 是同步的）"""
        if cls._progress is not None:
            cls._progress.stop() 
            cls._progress = None

    def __enter__(self):
        return self.get_progress()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._progress is not None:
            self._progress.__exit__(exc_type, exc_val, exc_tb)

    def format_description(self, message: Message) -> str:
        if self.loglevel == LogLevel.DEBUG and message.calling_file:
            class_part = f"{message.calling_class}." if message.calling_class else ""
            debug_prefix = f"[{message.calling_file}:{message.calling_line} in {class_part}{message.calling_func}]"
            formatter = f"{debug_prefix}{self.formatter}"
        else:
            formatter = self.formatter
        return formatter.format(**message.model_dump())

    async def push(self, message: Message):
        """根據訊息類型決定如何處理"""
        if self.loglevel == LogLevel.DEBUG:
            message.capture_caller_frame()
        
        # 日誌類型：直接輸出
        if message.msg_type == MessageType.LOG:
            if message.loglevel < self.loglevel:
                return
            await self.print_log(message)
        
        # 進度類型：創建新任務
        elif message.msg_type == MessageType.PROGRESS:
            new_id = self.get_progress().add_task("")
            message.task_id = int(new_id)
            await self.print_progress(message)
        
        # 進度更新類型：更新或完成任務
        elif message.msg_type == MessageType.PROGRESS_UPDATE:
            await self.print_progress(message)

    def format_message(self, message: Message) -> str:
        """格式化訊息字符串"""
        if self.loglevel == LogLevel.DEBUG and message.calling_file:
            class_part = f"{message.calling_class}." if message.calling_class else ""
            debug_prefix = f"[{message.calling_file}:{message.calling_line} in {class_part}{message.calling_func}]"
            formatter = f"{debug_prefix}{self.formatter}"
        else:
            formatter = self.formatter
        return formatter.format(**message.model_dump())

    async def print_log(self, message: Message):
        """輸出日誌訊息"""
        if message.message is not None:
            formatted = self.format_message(message)
            self.get_progress().console.log(formatted)

    async def print_progress(self, message: Message):
        """處理進度條訊息"""
        progress = self.get_progress()
        t_id = TaskID(message.task_id)
        formatted = self.format_message(message)

        # 情況 A：任務完成或錯誤
        if message.status in [Status.COMPLETED, Status.ERROR]:
            progress.remove_task(t_id)
            progress.console.log(formatted)
        
        # 情況 B：更新進度條
        else:
            update_kwargs = {}
            if message.message is not None:
                update_kwargs["description"] = formatted
            if message.completed is not None:
                update_kwargs["completed"] = message.completed
            if message.total is not None:
                update_kwargs["total"] = message.total
            if message.advance is not None:
                update_kwargs["advance"] = message.advance
            
            if update_kwargs:
                progress.update(t_id, **update_kwargs)

# 初始化：啟用 Debug 並註冊
Message.register_pusher(RichPusher())

if __name__ == "__main__":
    import asyncio
    async def task_simulation(task_id: int, total: int):
        """模擬一個下載任務，隨機更新進度"""
        for i in range(total):
            await Message.progress_update(
                task_id=task_id,
                advance=1,
                message="任務執行中"
            ).push()
            await asyncio.sleep(0.1)  # 模擬下載時間
        await Message.progress_complete(task_id=task_id, message="任務完成").push()
    
    async def main():
        # 測試日誌訊息
        await Message.log("開始測試", color=Color.BLUE).push()
        
        # 測試成功的進度條
        success_task = await Message.progress(
            message="成功任務",
            total=100,
            color=Color.GREEN
        ).push()
        await asyncio.sleep(0.2)
        await task_simulation(0, 50)  # 模擬進度更新
        
        await Message.log("任務完成", color=Color.GREEN, status=Status.COMPLETED).push()
        
        # 測試失敗的進度條
        error_task = Message.progress(
            message="失敗任務",
            total=100,
            color=Color.RED
        )
        await error_task.push()
        await asyncio.sleep(0.1)
        await Message.progress_error(
            task_id=error_task.task_id,
            message="發生錯誤"
        ).push()
        
        # 測試 shutdown 方法
        await RichPusher.shutdown()
    
    asyncio.run(main())
