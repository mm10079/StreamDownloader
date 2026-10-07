"""直接輸入串流 / 檔案網址時使用的通用提取器（對應舊版 '.m3u8' in url 的分支）"""
from pathlib import Path
from urllib.parse import urlsplit

from ...core.models import MediaJob, StreamKind, StreamSpec
from ...session import cookies as cookie_tools
from .._extractor import ExtractContext, ExtractorConfig, InfoExtractor

# 常見網站的預設 Referer（舊版 default_info.Common_Referers）
COMMON_REFERERS = {
    "pscp.tv": "https://x.com/",
    "video.twimg.com": "https://x.com/",
}

FILE_EXTS = {".mp4", ".m4a", ".mp3", ".aac", ".webm", ".mkv", ".flv", ".ts"}


class DirectExtractor(InfoExtractor):
    config = ExtractorConfig(
        name="direct",
        description="m3u8 / mpd / 一般媒體檔網址",
        valid_url_regex=r"^https?://",
        priority=1000,          # 最後才比對，讓網站專用提取器優先
    )

    @staticmethod
    def detect_kind(url: str) -> StreamKind | None:
        path = urlsplit(url).path.lower()
        if path.endswith(".m3u8") or ".m3u8" in url.lower():
            return StreamKind.HLS
        if path.endswith(".mpd"):
            return StreamKind.DASH
        if Path(path).suffix in FILE_EXTS:
            return StreamKind.FILE
        return None

    def match(self, url: str) -> bool:
        return super().match(url) and self.detect_kind(url) is not None

    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        opts = ctx.options
        host = urlsplit(url).hostname or ""
        referer = opts.referer or next((v for k, v in COMMON_REFERERS.items() if k in host), "")
        headers = {}
        if referer:
            headers["Referer"] = referer
            parts = urlsplit(referer)
            headers["Origin"] = f"{parts.scheme}://{parts.netloc}"

        session = ctx.new_session(headers)
        session.set_cookies(cookie_tools.load_user_cookies(opts.cookies, default_domain=host))

        return MediaJob(
            title=opts.title,
            streams=[StreamSpec(
                kind=self.detect_kind(url), url=url, title=opts.title,
                session_id=session.id, quality=opts.quality, backfill=self.config.backfill,
            )],
        )
