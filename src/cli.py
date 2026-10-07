import argparse
import asyncio
import signal
import sys
from pathlib import Path
from typing import Literal, get_args, get_origin, Optional, Union

from .core.options import Options
from .core.pipeline import Pipeline
from .utils import log
from .utils.console import RichPusher

SHORT = {"output": "-o", "title": "-t", "quality": "-q", "fetcher": "-f"}


def build_parser() -> argparse.ArgumentParser:
    """依 Options 欄位自動產生參數（沿用舊專案的作法）"""
    parser = argparse.ArgumentParser(prog="StreamDownloader", description="串流下載器")
    for name, field in Options.model_fields.items():
        if name == "url":
            parser.add_argument("url", nargs="?", default="", help="串流或網站網址")
            continue
        flags = [f"--{name.replace('_', '-')}"] + ([SHORT[name]] if name in SHORT else [])
        tp = field.annotation
        if get_origin(tp) is Union:     # Optional[X]
            tp = next(a for a in get_args(tp) if a is not type(None))
        default = field.get_default(call_default_factory=True)
        if name == "password":
            default = argparse.SUPPRESS     # 不在 --help 顯示環境變數中的密碼
        kwargs = {"help": field.description or "", "default": default}
        if tp is bool:
            kwargs["action"] = argparse.BooleanOptionalAction
        elif get_origin(tp) is Literal:
            kwargs["choices"] = list(get_args(tp))
        else:
            kwargs["type"] = tp if tp in (int, float, str, Path) else str
        parser.add_argument(*flags, **kwargs)
    return parser


def install_stop_handler(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """第一次 Ctrl+C：軟停止（停止追蹤直播、完成進行中下載並合併）；第二次：強制中斷"""
    def handler(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(stop.set)
        print("\n收到中斷訊號：停止追蹤新片段，等待進行中的下載完成（再按一次強制結束）")
    signal.signal(signal.SIGINT, handler)


async def amain(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    options = Options(**vars(args))
    if not options.url:
        options.url = input("請輸入網址：").strip()
    stop = asyncio.Event()
    install_stop_handler(asyncio.get_running_loop(), stop)
    try:
        ok = await Pipeline(options, stop).run(options.url)
    finally:
        await RichPusher.shutdown()
    if not ok:
        await log.warning("部分項目未完成")
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> None:
    try:
        sys.exit(asyncio.run(amain(argv)))
    except KeyboardInterrupt:
        print("已強制中斷；重新執行相同指令可從進度續傳")
        sys.exit(130)
