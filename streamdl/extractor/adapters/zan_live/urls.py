import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ZanUrls:
    """所有 ZAN-LIVE 網址集中於此；domain 可替換以便測試"""
    domain: str = "https://www.zan-live.com"
    lang: str = "/zh-TW"

    @property
    def login(self) -> str:
        return f"{self.domain}{self.lang}/auth/login"

    def detail(self, group_id: str | int) -> str:
        return f"{self.domain}{self.lang}/live/detail/{group_id}"

    def playroom(self, ticket_id: str | int, live_id: str | int) -> str:
        return f"{self.domain}{self.lang}/live/play/{ticket_id}/{live_id}"

    def tickets_api(self, group_id: str | int) -> str:
        return f"{self.domain}/api/live/detail/tickets?id={group_id}"

    def absolute(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return self.domain + "/" + url.lstrip("/")


# 語系前綴可有可無（/zh-TW、/ja、/en …）
DOMAIN_RE = r"zan-live\.com"
_DETAIL_RE = re.compile(DOMAIN_RE + r"/(?:[A-Za-z-]+/)?live/detail/(\d+)")
_PLAYROOM_RE = re.compile(DOMAIN_RE + r"/(?:[A-Za-z-]+/)?live/play/(\d+)/(\d+)")


def parse_detail(url: str) -> Optional[str]:
    m = _DETAIL_RE.search(url)
    return m.group(1) if m else None


def parse_playroom(url: str) -> Optional[tuple[str, str]]:
    m = _PLAYROOM_RE.search(url)
    return (m.group(1), m.group(2)) if m else None
