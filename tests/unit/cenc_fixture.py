"""測試用：把未加密的 fMP4（init + fragments）加密成符合 CENC 規範的格式

與 ffmpeg 不同，這裡會在每個 traf 寫入 senc（每個樣本的 IV 與 subsample），並在 sample entry 加入 sinf / tenc，
與 Shaka Packager、Bento4 等實際打包工具的輸出結構相同。
"""
import os
import struct
from dataclasses import dataclass, field
from typing import Optional

from Crypto.Cipher import AES
from Crypto.Util import Counter

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mvex", b"edts", b"dinf"}
ENTRY_PREFIX = {b"avc1": 78, b"hvc1": 78, b"hev1": 78, b"mp4a": 28}


@dataclass
class Box:
    type: bytes
    prefix: bytes = b""                 # 容器的固定欄位（stsd 的 8 位元組、sample entry 的固定欄位）
    data: bytes = b""
    children: Optional[list["Box"]] = None

    def find(self, typ: bytes) -> Optional["Box"]:
        return next((c for c in self.children or [] if c.type == typ), None)

    def serialize(self) -> bytes:
        body = self.prefix + (b"".join(c.serialize() for c in self.children) if self.children is not None else self.data)
        return struct.pack(">I4s", 8 + len(body), self.type) + body


def parse(buf: bytes, start: int = 0, end: Optional[int] = None) -> list[Box]:
    end = len(buf) if end is None else end
    boxes, off = [], start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, off)
        payload, box_end = off + 8, off + size
        if typ in CONTAINERS:
            boxes.append(Box(typ, children=parse(buf, payload, box_end)))
        elif typ == b"stsd":
            boxes.append(Box(typ, prefix=buf[payload: payload + 8], children=parse(buf, payload + 8, box_end)))
        elif typ in ENTRY_PREFIX:
            n = ENTRY_PREFIX[typ]
            boxes.append(Box(typ, prefix=buf[payload: payload + n], children=parse(buf, payload + n, box_end)))
        else:
            boxes.append(Box(typ, data=buf[payload: box_end]))
        off = box_end
    return boxes


def full_box(typ: bytes, version: int, flags: int, body: bytes) -> Box:
    return Box(typ, data=bytes([version]) + flags.to_bytes(3, "big") + body)


def walk(boxes: list[Box]):
    for b in boxes:
        yield b
        if b.children:
            yield from walk(b.children)


@dataclass
class Scheme:
    name: str = "cenc"                  # cenc / cbcs
    key: bytes = bytes(range(16))
    kid: bytes = bytes(range(16, 32))
    iv_size: int = 8                    # cbcs 使用 0（constant IV）
    constant_iv: bytes = b""
    crypt: int = 0
    skip: int = 0
    clear_lead: int = 5                 # 每個樣本開頭保留的明文位元組（模擬 NAL header）
    ivs: list[bytes] = field(default_factory=list)


def encrypt_init(init: bytes, s: Scheme) -> bytes:
    boxes = parse(init)
    for b in walk(boxes):
        if b.type in ENTRY_PREFIX:
            original = b.type
            b.type = b"encv" if original != b"mp4a" else b"enca"
            pattern = (s.crypt << 4) | s.skip
            tenc_body = bytes([0, pattern, 1, s.iv_size]) + s.kid
            if s.iv_size == 0:
                tenc_body += bytes([len(s.constant_iv)]) + s.constant_iv
            sinf = Box(b"sinf", children=[
                Box(b"frma", data=original),
                full_box(b"schm", 0, 0, s.name.encode() + struct.pack(">I", 0x10000)),
                Box(b"schi", children=[full_box(b"tenc", 1 if pattern else 0, 0, tenc_body)]),
            ])
            b.children.append(sinf)
    return b"".join(b.serialize() for b in boxes)


def _encrypt_sample(sample: bytes, s: Scheme, iv: bytes) -> tuple[bytes, list[tuple[int, int]]]:
    clear = min(s.clear_lead, len(sample))
    protected = len(sample) - clear
    out = bytearray(sample)
    if s.name == "cenc":
        ctr = Counter.new(128, initial_value=int.from_bytes(iv.ljust(16, b"\0"), "big"))
        out[clear:] = AES.new(s.key, AES.MODE_CTR, counter=ctr).encrypt(bytes(out[clear:]))
    else:   # cbcs pattern
        cipher = AES.new(s.key, AES.MODE_CBC, iv=s.constant_iv.ljust(16, b"\0"))
        pos, end = clear, len(sample)
        crypt, skip = (s.crypt, s.skip) if s.crypt else (1, 0)
        while pos + 16 <= end:
            n = min(crypt, (end - pos) // 16)
            out[pos: pos + n * 16] = cipher.encrypt(bytes(out[pos: pos + n * 16]))
            pos += (n + skip) * 16
    return bytes(out), [(clear, protected)]


def encrypt_segment(segment: bytes, s: Scheme) -> bytes:
    boxes = parse(segment)
    out = b""
    i = 0
    while i < len(boxes):
        b = boxes[i]
        if b.type != b"moof":
            out += b.serialize()
            i += 1
            continue
        moof, mdat = b, boxes[i + 1]
        traf = moof.find(b"traf")
        trun = traf.find(b"trun")
        flags = int.from_bytes(trun.data[1:4], "big")
        count = struct.unpack_from(">I", trun.data, 4)[0]
        off = 8 + (4 if flags & 1 else 0) + (4 if flags & 4 else 0)
        sizes = []
        for _ in range(count):
            off += 4 if flags & 0x100 else 0
            sizes.append(struct.unpack_from(">I", trun.data, off)[0])
            off += 4 + (4 if flags & 0x400 else 0) + (4 if flags & 0x800 else 0)
        old_moof_len = len(moof.serialize())
        old_offset = struct.unpack_from(">i", trun.data, 8)[0]
        pos = old_offset - old_moof_len - 8         # 樣本在 mdat 內容中的位置
        data = bytearray(mdat.data)
        senc = b""
        for size in sizes:
            iv = os.urandom(s.iv_size) if s.iv_size else b""
            s.ivs.append(iv)
            enc, subs = _encrypt_sample(bytes(data[pos: pos + size]), s, iv)
            data[pos: pos + size] = enc
            senc += iv + struct.pack(">H", len(subs)) + b"".join(struct.pack(">HI", c, p) for c, p in subs)
            pos += size
        mdat.data = bytes(data)
        traf.children.append(full_box(b"senc", 0, 0x2, struct.pack(">I", count) + senc))
        # moof 變大 → 更新 trun.data_offset
        delta = len(moof.serialize()) - old_moof_len
        trun.data = trun.data[:8] + struct.pack(">i", old_offset + delta) + trun.data[12:]
        out += moof.serialize() + mdat.serialize()
        i += 2
    return out


# ---------------- 合成 fMP4（單元測試用，不需要 ffmpeg） ----------------

def synthetic_fmp4(samples: list[list[bytes]], track_id: int = 1, entry: bytes = b"avc1") -> tuple[bytes, list[bytes]]:
    """回傳 (init, [每個 fragment])；samples 為每個 fragment 的樣本內容"""
    tkhd = full_box(b"tkhd", 0, 3, bytes(8) + struct.pack(">I", track_id) + bytes(68))
    sample_entry = Box(entry, prefix=bytes(ENTRY_PREFIX[entry]), children=[Box(b"btrt", data=bytes(12))])
    stsd = Box(b"stsd", prefix=bytes(4) + struct.pack(">I", 1), children=[sample_entry])
    moov = Box(b"moov", children=[
        full_box(b"mvhd", 0, 0, bytes(96)),
        Box(b"trak", children=[tkhd, Box(b"mdia", children=[Box(b"minf", children=[Box(b"stbl", children=[stsd])])])]),
        Box(b"mvex", children=[full_box(b"trex", 0, 0, struct.pack(">IIIII", track_id, 1, 0, 0, 0))]),
    ])
    init = Box(b"ftyp", data=b"isom" + bytes(4)).serialize() + moov.serialize()
    fragments = []
    for n, frag in enumerate(samples):
        trun_body = struct.pack(">I", len(frag)) + struct.pack(">i", 0) + b"".join(struct.pack(">I", len(x)) for x in frag)
        trun = full_box(b"trun", 0, 0x201, trun_body)
        tfhd = full_box(b"tfhd", 0, 0x020000, struct.pack(">I", track_id))      # default-base-is-moof
        moof = Box(b"moof", children=[full_box(b"mfhd", 0, 0, struct.pack(">I", n + 1)),
                                      Box(b"traf", children=[tfhd, full_box(b"tfdt", 0, 0, bytes(4)), trun])])
        offset = len(moof.serialize()) + 8
        trun.data = trun.data[:8] + struct.pack(">i", offset) + trun.data[12:]
        fragments.append(moof.serialize() + Box(b"mdat", data=b"".join(frag)).serialize())
    return init, fragments
