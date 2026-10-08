"""StreamDownloader：HLS / DASH 串流下載器

    import streamdl
    result = streamdl.download("https://example.com/master.m3u8", output="downloads")
"""
__version__ = "0.4.0"

from .api import adownload, download
from .core.options import Options
from .core.result import AttachmentOutcome, DownloadResult, StreamOutcome

__all__ = ["download", "adownload", "Options", "DownloadResult", "StreamOutcome", "AttachmentOutcome",
           "__version__", "main"]


def main():
    """命令列進入點（streamdl 指令 / python -m streamdl）"""
    from .cli import main as cli_main
    cli_main()
