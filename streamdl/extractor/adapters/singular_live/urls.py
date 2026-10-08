import re
from dataclasses import dataclass
from typing import Optional

ULIZA_PLAYER = "https://player-api.p.uliza.jp/v1/players/player_05/thinkr/admin?name={name}&playerobjectname=player"


@dataclass(frozen=True)
class SingularUrls:
    """所有 SINGULAR LIVE 網址集中於此；domain / uliza 可替換以便測試"""
    domain: str = "https://singular-live.thinkr.jp"
    lang: str = "zh"
    uliza: str = ULIZA_PLAYER

    @property
    def login(self) -> str:
        return f"{self.domain}/{self.lang}/login"

    def detail(self, event_id: str) -> str:
        return f"{self.domain}/{self.lang}/event/detail/{event_id}"

    def play(self, ticket_id: str, content_id: str) -> str:
        return f"{self.domain}/{self.lang}/play/{ticket_id}/{content_id}"

    def available_tickets(self, limit: int = 200) -> str:
        return f"{self.domain}/{self.lang}/api/mypage/available-tickets?limit={limit}&scope=watchable"

    def uliza_params(self, uliza_content_id: str) -> str:
        return self.uliza.format(name=uliza_content_id)

    def absolute(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return self.domain + "/" + url.lstrip("/")


DOMAIN_RE = r"singular-live\.thinkr\.jp"
_LANG = r"/(?P<lang>[a-z]{2}(?:-[A-Za-z]+)?)"
_DETAIL_RE = re.compile(DOMAIN_RE + _LANG + r"/event/detail/(?P<id>[0-9a-fA-F-]{8,})")
_PLAY_RE = re.compile(DOMAIN_RE + _LANG + r"/play/(?P<ticket>[0-9a-fA-F-]{8,})/(?P<content>[0-9a-fA-F-]{8,})")


def parse_detail(url: str) -> Optional[tuple[str, str]]:
    """(lang, event_id)"""
    m = _DETAIL_RE.search(url)
    return (m.group("lang"), m.group("id")) if m else None


def parse_play(url: str) -> Optional[tuple[str, str, str]]:
    """(lang, ticket_id, content_id)"""
    m = _PLAY_RE.search(url)
    return (m.group("lang"), m.group("ticket"), m.group("content")) if m else None
