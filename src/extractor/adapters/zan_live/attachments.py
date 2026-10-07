"""附件清單（舊版 ZanAttachment.create_download_list）

資料夾結構（相對於 job 輸出資料夾）：
{直播名稱}/
├── 封面、宣傳圖
├── images/logos/       Logo、Banner
├── images/artists/     參演者照片
├── images/gifts/       禮物圖示
├── raw comments/{直播名稱}/  留言 JSON
└── web info/
    ├── gifts/          各類禮物 JSON
    └── tickets/        liveTickets / ticketGroupArtists / live-banners JSON、縮圖 VTT
"""
from pathlib import Path
from typing import Iterable, Optional

from ....core.models import AttachmentKind, AttachmentSpec
from ....utils.paths import sanitize_filename, url_basename
from .schema.item import ArtistDetail, BannerItem, GiftItem, LiveRoomMetas
from .urls import ZanUrls


class AttachmentBuilder:
    def __init__(self, urls: ZanUrls, live_name: str, session_id: str):
        self.urls = urls
        self.session_id = session_id
        root = Path(sanitize_filename(live_name))
        self.root = root
        self.logos = root / "images" / "logos"
        self.artists = root / "images" / "artists"
        self.gifts = root / "images" / "gifts"
        self.comments = root / "raw comments" / sanitize_filename(live_name)
        self.gifts_json = root / "web info" / "gifts"
        self.tickets_json = root / "web info" / "tickets"
        self.items: list[AttachmentSpec] = []
        self._seen: set[str] = set()

    def url(self, folder: Path, url: Optional[str]) -> None:
        url = self.urls.absolute(url)
        if not url or url in self._seen:
            return
        self._seen.add(url)
        name = url_basename(url) or "file"
        self.items.append(AttachmentSpec(kind=AttachmentKind.URL, path=folder / name, url=url, session_id=self.session_id))

    def json(self, path: Path, data) -> None:
        self.items.append(AttachmentSpec(kind=AttachmentKind.JSON, path=path, data=data))

    def build(
        self,
        metas: LiveRoomMetas,
        cover: Optional[str],
        top_images: Iterable[str],
        gifts: dict[str, list[GiftItem]],
        tickets: list[dict],
        artists: list[ArtistDetail],
        comments: list[str],
    ) -> list[AttachmentSpec]:
        for u in [cover, metas.MultiangleThumbnailUrl, *top_images]:
            self.url(self.root, u)
        for u in (metas.Livelogourl, metas.DesignTitleImageURL, metas.DesignTaptostartImage):
            self.url(self.logos, u)

        banners: list[BannerItem] = metas.LiveBanners or []
        for b in banners:
            self.url(self.logos, b.bannerImageURL)
        if banners:
            self.json(self.tickets_json / "live-banners.json", [b.model_dump() for b in banners])

        for label, items in gifts.items():
            for g in items:
                self.url(self.gifts, g.listIconUrl)
                self.url(self.gifts, g.backgroundImage)
            self.json(self.gifts_json / f"{label}.json", [g.model_dump() for g in items])

        self.json(self.tickets_json / "liveTickets.json", tickets)
        for a in artists:
            self.url(self.artists, a.image_url)
        self.json(self.tickets_json / "ticketGroupArtists.json", [a.model_dump() for a in artists])

        for u in comments:
            self.url(self.comments, u)
        if metas.SeekBarthumbnailVttUrl:
            self.url(self.tickets_json, metas.SeekBarthumbnailVttUrl)
        return self.items
