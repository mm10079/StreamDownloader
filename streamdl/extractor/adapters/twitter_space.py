"""Twitter（X）Space：直接下載 pscp.tv 的 m3u8（目前不含瀏覽器擷取）

- 只需 Referer / Origin 設為 x.com（目前伺服器不檢查，保留以防日後改變）
- master_playlist 的各個 variant 都是 AAC 音訊（CODECS 標示含 avc1 但實際為音訊），合併輸出 .m4a
- 資料夾沿用舊版命名：{播出日期} - Twitter Space #{標題}；播出日期取自播放清單的
  EXT-X-PROGRAM-DATE-TIME（舊版寫死日期）
- 片段名稱 chunk_{時間戳}_{序號}_a.aac 有兩組變動數字，無法推出回溯模板；存檔本身即完整，因此關閉回溯
"""
import re
from datetime import datetime
from typing import Optional

from ...core.models import MediaJob, StreamKind, StreamSpec
from ...fetcher import request_text
from ...protocol.hls import parser as hls
from ...session import Session
from ...session import cookies as cookie_tools
from ...utils import log
from ...utils.paths import sanitize_filename
from .._extractor import ExtractContext, ExtractorConfig, InfoExtractor

REFERER = "https://x.com/"
DEFAULT_TITLE = "Twitter Space"
_PDT = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)")


def folder_name(title: str, broadcast: Optional[datetime]) -> str:
    date = (broadcast or datetime.now().astimezone()).astimezone().strftime("%Y%m%d")
    tag = title if title.startswith("#") else f"#{title}"
    return sanitize_filename(f"{date} - Twitter Space {tag}")


class TwitterSpaceExtractor(InfoExtractor):
    config = ExtractorConfig(
        name="twitter-space",
        description="Twitter（X）Space 錄音 / 直播（pscp.tv m3u8）",
        valid_url_regex=r"^https?://[^/]*\.pscp\.tv/.+\.m3u8",
        priority=10,
        backfill=False,
    )

    async def extract(self, url: str, ctx: ExtractContext) -> MediaJob:
        opts = ctx.options
        session = ctx.new_session({"Referer": opts.referer or REFERER, "Origin": "https://x.com"})
        if opts.cookies:
            session.set_cookies(cookie_tools.load_user_cookies(opts.cookies, default_domain=".pscp.tv"))

        title = opts.title
        if title in ("", "media"):
            title = await log.ask(f"Space 標題（直接 Enter 使用「{DEFAULT_TITLE}」）：", default=DEFAULT_TITLE)
        title = sanitize_filename(title.strip() or DEFAULT_TITLE)

        broadcast, live = await self._inspect(session, url)
        folder = folder_name(title, broadcast)
        when = f"{broadcast.astimezone():%Y-%m-%d %H:%M}" if broadcast else "未知"
        await log.info(f"Twitter Space：{title}（{'直播中' if live else '錄音'}，開始時間 {when}）")

        return MediaJob(
            title=title,
            output_dir=opts.output / folder,
            streams=[StreamSpec(kind=StreamKind.HLS, url=url, title=title, session_id=session.id,
                                quality=opts.quality, backfill=self.config.backfill)],
        )

    @staticmethod
    async def _inspect(session: Session, url: str) -> tuple[Optional[datetime], bool]:
        """讀取播放清單：(第一個片段的 PROGRAM-DATE-TIME, 是否為直播)；失敗時交給下載階段回報"""
        try:
            text, _ = await request_text(session, url)
            if text and hls.is_master(text):
                master = hls.parse_master(text, url)
                if master.variants:
                    text, _ = await request_text(session, master.variants[0].uri)
            if not text:
                return None, False
            m = _PDT.search(text)
            broadcast = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")) if m else None
            live = "#EXT-X-ENDLIST" not in text and "PLAYLIST-TYPE:VOD" not in text
            return broadcast, live
        except Exception:
            return None, False
