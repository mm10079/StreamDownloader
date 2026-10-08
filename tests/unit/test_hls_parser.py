from streamdl.protocol.hls import parser
from streamdl.protocol.hls.decrypt import resolve_iv

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=8360000,CODECS="avc1.4d4028,mp4a.40.2",DISPLAY-NAME="1080p"
index_2.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=27808000,CODECS="avc1.4d4033,mp4a.40.2",DISPLAY-NAME="2160p"
index_1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2640000,DISPLAY-NAME="480p"
/abs/index_4.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:1
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="https://www.zan-live.com/api/vod/3291/getKey",IV=0x3DEFA6D2DC885ED5223B4D4B7C50A72F
#EXTINF:6.00000,
index_1/00000/index_1_01669.ts
#EXTINF:6.00000,
index_1/00000/index_1_01670.ts
#EXT-X-KEY:METHOD=AES-128,URI="key2"
#EXTINF:1.03333,
index_1/00000/index_1_01671.ts
#EXT-X-ENDLIST
"""


def test_master_sorted_by_bandwidth_and_resolved():
    m = parser.parse_master(MASTER, "https://cdn.example.com/live/a/master.m3u8?t=1")
    assert [v.name for v in m.variants] == ["2160p", "1080p", "480p"]
    assert m.variants[0].uri == "https://cdn.example.com/live/a/index_1.m3u8"
    assert m.variants[2].uri == "https://cdn.example.com/abs/index_4.m3u8"
    assert parser.is_master(MASTER) and not parser.is_master(MEDIA)


def test_media_segments_keys_and_sequence():
    pl = parser.parse_media(MEDIA, "https://cdn.example.com/vod/index_1.m3u8")
    assert pl.endlist and not pl.is_live
    assert [s.media_sequence for s in pl.segments] == [1, 2, 3]
    assert pl.segments[0].uri == "https://cdn.example.com/vod/index_1/00000/index_1_01669.ts"
    assert pl.segments[0].key.iv == "3DEFA6D2DC885ED5223B4D4B7C50A72F"
    assert pl.segments[2].key.iv is None
    assert pl.segments[2].key.uri == "https://cdn.example.com/vod/key2"
    assert pl.header_lines == ["#EXT-X-VERSION:3"]


def test_byterange():
    text = "#EXTM3U\n#EXTINF:1,\n#EXT-X-BYTERANGE:100@0\nall.ts\n#EXTINF:1,\n#EXT-X-BYTERANGE:50\nall.ts\n"
    pl = parser.parse_media(text, "https://a/b.m3u8")
    assert [s.byte_range for s in pl.segments] == [(0, 99), (100, 149)]


def test_implicit_iv_uses_media_sequence():
    assert resolve_iv(None, 1) == "00000000000000000000000000000001"
    assert resolve_iv("0xABC", 5) == "00000000000000000000000000000abc"


def test_legacy_base_candidates():
    c = parser.legacy_base_candidates("https://h/a/b/c/index.m3u8", "c/seg1.ts")
    assert c[0] == "https://h/a/b/c/c/seg1.ts"
    assert "https://h/a/b/c/seg1.ts" in c
