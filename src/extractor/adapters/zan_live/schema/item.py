from typing import Optional, List, Any
from pydantic import BaseModel, Field, HttpUrl
from enum import Enum
import os


class MetaLabels(str, Enum):
    NAME = "name"
    PROPERTY = "property"

class MetaName:
    # 變數區域
    CSRF_TOKEN = 'csrf_token'
    OPEN_LIVE_DATE = 'open_live_date'
    TICKET_GROUP_ID = 'ticket_group_id'
    TICKET_ID = 'ticket_id'
    LIVE_ID = 'live_id'
    LIVE_NAME = 'live_name'
    TICKET_URL = 'live_url'

class TicketIDs(BaseModel):
    group_id: str
    ticket_id: Optional[str]

class GiftItem(BaseModel):
    id: str
    liveId: Optional[str]
    name: str
    consumePoint: int
    iconUrl: str
    commentIconUrl: str
    listIconUrl: str
    isCharged: bool
    backgroundColor: str
    backgroundImage: str
    backgroundImageRepeat: bool
    nameFontColor: str
    giftFontColor: str
    detailFontColor: str
    detailBackColor: str
    borderColor: str
    comIconPosRight: int
    comIconPosTop: int
    comIconScale: float
    priority: int
    isValid: bool
    group: int
    displayTime: int
    usageText: str
    createdAt: str
    updatedAt: str

class BannerItem(BaseModel):
    bannerId: str
    bannerImageURL: str
    bannerLinkURL: str
    bannerOverImageURL: str
    bannerHorizontalWidth: int
    bannerVerticalWidth: int



class ArtistLink(BaseModel):
    """藝術家相關連結（如 Twitter, YouTube, 官網）"""
    id: str
    type: int
    text: str
    url: str  # 這裡使用 str 避免 Pylance 靜態報錯
    image_url: str = Field(alias="imageUrl")
    display_order: int = Field(alias="displayOrderDesc")

class ArtistDetail(BaseModel):
    """藝術家/團體主體資訊"""
    id: str
    name: str
    detail: str  # 這裡會包含 HTML 字串
    image_url: str = Field(alias="imageUrl")
    display_order: int = Field(alias="displayOrderDesc")
    links: List[ArtistLink]



class TicketDetail(BaseModel):
    # 購票資訊
    id: str
    name: str # 票券名稱
    liveName: Optional[str] # 直播名稱
    liveId: Optional[int]
    originLiveId: Optional[int]
    isLive: bool
    openLiveDate: Optional[str]
    liveBeginDate: Optional[str]
    liveEndDate: Optional[str]
    isArchiveStream: bool # 是否為存檔
    haveViewableTicket: bool # 是否為可觀看的票券
    isShowStreamBeginDate: bool # 是否顯示在detail頁面上

class LiveRoomMetas(BaseModel):
    # 基本資訊
    Livename: str = Field(default=..., json_schema_extra={"labal": MetaLabels.NAME, "key": "live-name"})
    Liveurl: str = Field(default=..., json_schema_extra={"labal": MetaLabels.NAME, "key": "live-url"})
    Livelogourl: Optional[str] = Field(default=None, json_schema_extra={"labal": MetaLabels.NAME, "key": "live-logo-url"})
    
    # 禮物類別 (使用強型別 List[GiftItem])
    NormalGifts: Optional[List[GiftItem]] = Field(default_factory=list, json_schema_extra={"labal": MetaLabels.NAME, "key": "normalGifts"})
    SpecialGifts: Optional[List[GiftItem]] = Field(default_factory=list, json_schema_extra={"labal": MetaLabels.NAME, "key": "specialGifts"})
    ComboFinishGifts: Optional[List[GiftItem]] = Field(default_factory=list, json_schema_extra={"labal": MetaLabels.NAME, "key": "comboFinishGifts"})
    
    # 存檔/重播專用禮物清單
    ArchiveCommentNormalGiftList: Optional[List[GiftItem]] = Field(default_factory=list, json_schema_extra={"labal": MetaLabels.NAME, "key": "archive-comment-normal-gift-list"})
    ArchiveCommentComboFinishGiftList: Optional[List[GiftItem]] = Field(default_factory=list, json_schema_extra={"labal": MetaLabels.NAME, "key": "archive-comment-combo-finish-gift-list"})
    
    # 其他資源
    SeekBarthumbnailVttUrl: Optional[str] = Field(default=None, json_schema_extra={"lebal": MetaLabels.NAME, "key": "seek-bar-thumbnail-vtt-url"})
    VodCommentManifestUrl: Optional[str] = Field(default=None, json_schema_extra={"lebal": MetaLabels.NAME, "key": "vod-comment-manifest-url"})
    DesignTitleImageURL: Optional[str] = Field(default=None, json_schema_extra={"labal": MetaLabels.NAME, "key": "design-titleImageURL"})
    DesignTaptostartImage: Optional[str] = Field(default=None, json_schema_extra={"labal": MetaLabels.NAME, "key": "design-taptostartImage"})
    MultiangleThumbnailUrl: Optional[str] = Field(default=None, json_schema_extra={"labal": MetaLabels.NAME, "key": "multiangle-thumbnail-url"})
    LiveBanners: List[BannerItem] = Field(default_factory=list, json_schema_extra={"labal": MetaLabels.NAME, "key": "live-banners"})

class DetailMetas(BaseModel):
    Title: Optional[str] = Field(default=None, json_schema_extra={"labal": MetaLabels.PROPERTY, "key": "og:title"})
    Image: Optional[str] = Field(default=None, json_schema_extra={"labal": MetaLabels.PROPERTY, "key": "og:image"})



class Attachment(BaseModel):
    # 封面圖片，這兩個相同
    CoverImageURL: Optional[str] = Field(default="", json_schema_extra={})
    MultiangleThumbnailUrl: Optional[str] = Field(default="", json_schema_extra={})
    # Logo圖片
    Livelogourl: Optional[str] = Field(default="", json_schema_extra={}) # 未知
    DesignTitleImageURL: Optional[str] = Field(default="", json_schema_extra={}) # 標題圖片
    DesignTaptostartImage: Optional[str] = Field(default="", json_schema_extra={}) # 暫停圖片
    # Json
    LiveBanners: List[BannerItem] = Field(default_factory=list, json_schema_extra={})
    GiftsItems: dict[str, List[GiftItem]] = Field(default_factory=dict, json_schema_extra={})
    LiveTickets: List[dict[str, Any]] = Field(default_factory=list, json_schema_extra={})
    TicketGroupArtists: List[ArtistDetail] = Field(default_factory=list, json_schema_extra={})
    CommentURLs: List[str] = Field(default_factory=list, json_schema_extra={})
    SeekBarthumbnailVttUrl: Optional[str] = Field(default=None, json_schema_extra={})
    # 非Metas圖片
    TopTitleImages: List[str] = Field(default_factory=list, json_schema_extra={}) # 特殊售票頁面宣傳圖片
    OthersImages: List[str] = Field(default_factory=list, json_schema_extra={}) # 其餘圖片



class FolderFormat(BaseModel):
    Root: str
    ImagesRoot: str
    RoomsImages: str
    ArtistsImages: str
    GiftsImages: str
    Comments: str
    Gifts_Json: str
    Tickets_Json: str

    @classmethod
    def from_path(cls, output: str, title: str):
        Root = os.path.join(output, title)
        ImagesRoot = os.path.join(Root, 'images')
        RoomsImages = os.path.join(ImagesRoot, 'logos')
        ArtistsImages = os.path.join(ImagesRoot, 'artists')
        GiftsImages = os.path.join(ImagesRoot, 'gifts')
        Comments = os.path.join(Root, 'raw comments', title)
        Gifts_Json = os.path.join(Root, 'web info', 'attachments')
        Tickets_Json = os.path.join(Root, 'web info', 'tickets')

        return cls(
            Root=Root,
            ImagesRoot=ImagesRoot,
            RoomsImages=RoomsImages,
            ArtistsImages=ArtistsImages,
            GiftsImages=GiftsImages,
            Comments=Comments,
            Gifts_Json=Gifts_Json,
            Tickets_Json=Tickets_Json,
        )