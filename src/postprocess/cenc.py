"""CENC（ISO/IEC 23001-7）解密：只適用於已持有金鑰的內容（--key 或 ClearKey）

- 優先使用 Bento4 的 mp4decrypt（若已安裝）
- 否則使用內建的 Python 實作：支援 cenc（AES-CTR）與 cbcs（AES-CBC pattern），含 subsample
  逐個 fragment 處理（moof + mdat），大型檔案不需整個載入記憶體

處理方式（不改變任何 box 大小，因此不需重算偏移）
- init：encv / enca 改回 frma 記錄的原始格式；sinf 改為 free
- fragment：依 senc 的 IV 與 subsample 原地解密 mdat 中的樣本；senc / saiz / saio 改為 free
"""
import asyncio
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from Crypto.Cipher import AES
from Crypto.Util import Counter

from ..utils import log
from .ffmpeg import find_ffmpeg
from .progress import run_in_thread, size

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mvex", b"edts", b"dinf", b"schi"}
PIFF_SENC = bytes.fromhex("a2394f525a9b4f14a2446c427c648df4")    # Smooth Streaming / PIFF 的 senc


class CencError(Exception):
    pass


@dataclass
class TrackCrypto:
    scheme: str             # cenc / cbcs / cens / cbc1
    kid: str                # hex
    iv_size: int
    constant_iv: Optional[bytes]
    crypt: int = 0          # pattern（cbcs / cens）
    skip: int = 0
    key: Optional[bytes] = None


# ---------------- box 工具 ----------------

def _boxes(buf: bytearray, start: int, end: int):
    """依序產生 (type, box 起點, 內容起點, box 終點)"""
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, off)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, off + 8)[0]
            header = 16
        elif size == 0:
            size = end - off
        if size < header or off + size > end:
            raise CencError(f"損壞的 box：{typ!r} @ {off}")
        if typ == b"uuid":
            header += 16
        yield typ, off, off + header, off + size
        off += size


def _rename(buf: bytearray, box_start: int, new_type: bytes) -> None:
    buf[box_start + 4: box_start + 8] = new_type


# ---------------- 樣本解密（核心） ----------------

def decrypt_sample(data: bytes, track: TrackCrypto, iv: bytes, subsamples: list[tuple[int, int]]) -> bytes:
    """解密單一樣本。subsamples 為 [(明文位元組數, 加密位元組數), …]，空則整個樣本加密"""
    if not subsamples:
        subsamples = [(0, len(data))]
    key = track.key
    out = bytearray(data)
    if track.scheme == "cenc":
        # AES-CTR：所有加密區段串成一條連續的金鑰流
        iv16 = iv.ljust(16, b"\x00")
        ctr = Counter.new(128, initial_value=int.from_bytes(iv16, "big"))
        cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
        pos = 0
        for clear, protected in subsamples:
            pos += clear
            out[pos: pos + protected] = cipher.decrypt(bytes(out[pos: pos + protected]))
            pos += protected
        return bytes(out)
    if track.scheme == "cbcs":
        # AES-CBC pattern：每個加密區段以相同 IV 重新開始；每 (crypt + skip) 個區塊解密前 crypt 個；不足 16 位元組的尾端為明文
        crypt, skip = (track.crypt, track.skip) if track.crypt else (1, 0)
        pos = 0
        for clear, protected in subsamples:
            pos += clear
            cipher = AES.new(key, AES.MODE_CBC, iv=iv.ljust(16, b"\x00"))
            block = pos
            end = pos + protected
            while block + 16 <= end:
                n = min(crypt, (end - block) // 16)
                out[block: block + n * 16] = cipher.decrypt(bytes(out[block: block + n * 16]))
                block += (n + skip) * 16
            pos = end
        return bytes(out)
    raise CencError(f"不支援的加密方案：{track.scheme}")


# ---------------- init（moov） ----------------

def _parse_tenc(buf: bytearray, payload: int) -> tuple[int, int, int, str, Optional[bytes]]:
    version = buf[payload]
    crypt = skip = 0
    if version >= 1:
        pattern = buf[payload + 5]
        crypt, skip = pattern >> 4, pattern & 0x0F
    iv_size = buf[payload + 7]
    kid = bytes(buf[payload + 8: payload + 24]).hex()
    constant = None
    if iv_size == 0:
        n = buf[payload + 24]
        constant = bytes(buf[payload + 25: payload + 25 + n])
    return crypt, skip, iv_size, kid, constant


def process_moov(buf: bytearray, start: int, end: int) -> dict[int, TrackCrypto]:
    """解析並原地修改 moov：回傳 {track_ID: TrackCrypto}"""
    tracks: dict[int, TrackCrypto] = {}

    def walk(s: int, e: int, track_id: Optional[int]) -> None:
        for typ, box, payload, box_end in _boxes(buf, s, e):
            if typ == b"trak":
                tid = None
                for t2, _, p2, _ in _boxes(buf, payload, box_end):
                    if t2 == b"tkhd":
                        version = buf[p2]
                        tid = struct.unpack_from(">I", buf, p2 + (20 if version == 1 else 12))[0]
                walk(payload, box_end, tid)
            elif typ in CONTAINERS:
                walk(payload, box_end, track_id)
            elif typ == b"stsd":
                walk(payload + 8, box_end, track_id)     # version/flags + entry_count
            elif typ in (b"encv", b"enca"):
                inner = payload + (78 if typ == b"encv" else 28)    # VisualSampleEntry / AudioSampleEntry 固定欄位
                for t2, b2, p2, e2 in _boxes(buf, inner, box_end):
                    if t2 != b"sinf":
                        continue
                    original = scheme = None
                    tenc = None
                    for t3, _, p3, e3 in _boxes(buf, p2, e2):
                        if t3 == b"frma":
                            original = bytes(buf[p3: p3 + 4])
                        elif t3 == b"schm":
                            scheme = bytes(buf[p3 + 4: p3 + 8]).decode("latin1")
                        elif t3 == b"schi":
                            for t4, _, p4, _ in _boxes(buf, p3, e3):
                                if t4 == b"tenc":
                                    tenc = _parse_tenc(buf, p4)
                    if original is None or tenc is None:
                        raise CencError("加密的 sample entry 缺少 frma 或 tenc")
                    crypt, skip, iv_size, kid, constant = tenc
                    tracks[track_id or 1] = TrackCrypto(scheme or "cenc", kid, iv_size, constant, crypt, skip)
                    _rename(buf, box, original)     # encv → avc1 等
                    _rename(buf, b2, b"free")       # 移除 sinf
            elif typ == b"pssh":
                _rename(buf, box, b"free")

    walk(start, end, None)
    return tracks


# ---------------- fragment（moof + mdat） ----------------

def _parse_senc(buf: bytearray, payload: int, iv_size: int) -> list[tuple[bytes, list[tuple[int, int]]]]:
    flags = int.from_bytes(buf[payload + 1: payload + 4], "big")
    count = struct.unpack_from(">I", buf, payload + 4)[0]
    off = payload + 8
    entries = []
    for _ in range(count):
        iv = bytes(buf[off: off + iv_size])
        off += iv_size
        subs = []
        if flags & 0x2:
            n = struct.unpack_from(">H", buf, off)[0]
            off += 2
            for _ in range(n):
                clear, protected = struct.unpack_from(">HI", buf, off)
                subs.append((clear, protected))
                off += 6
        entries.append((iv, subs))
    return entries


def process_fragment(buf: bytearray, moof_start: int, moof_end: int, tracks: dict[int, TrackCrypto],
                     trex_defaults: dict[int, int]) -> None:
    """buf 從 moof 開始，包含其後的 mdat；樣本位置 = moof 起點 + trun.data_offset（default-base-is-moof）"""
    for typ, _, payload, traf_end in _boxes(buf, moof_start + 8, moof_end):
        if typ != b"traf":
            continue
        track_id = None
        base = moof_start       # 規範：未指定 base_data_offset 時以所屬 moof 起點為基準
        default_size = 0
        sizes: list[int] = []
        data_offset = 0
        senc = None
        for t2, b2, p2, e2 in _boxes(buf, payload, traf_end):
            if t2 == b"tfhd":
                flags = int.from_bytes(buf[p2 + 1: p2 + 4], "big")
                track_id = struct.unpack_from(">I", buf, p2 + 4)[0]
                off = p2 + 8
                if flags & 0x01:
                    # 絕對位置的 base_data_offset 無法在逐 fragment 處理時對應（DASH 幾乎都用 default-base-is-moof）
                    raise CencError("不支援 tfhd base_data_offset，請安裝 Bento4 的 mp4decrypt")
                if flags & 0x02:
                    off += 4
                if flags & 0x08:
                    off += 4
                if flags & 0x10:
                    default_size = struct.unpack_from(">I", buf, off)[0]
                    off += 4
            elif t2 == b"trun":
                flags = int.from_bytes(buf[p2 + 1: p2 + 4], "big")
                count = struct.unpack_from(">I", buf, p2 + 4)[0]
                off = p2 + 8
                if flags & 0x01:
                    data_offset = struct.unpack_from(">i", buf, off)[0]
                    off += 4
                if flags & 0x04:
                    off += 4
                for _ in range(count):
                    if flags & 0x100:
                        off += 4
                    if flags & 0x200:
                        sizes.append(struct.unpack_from(">I", buf, off)[0])
                        off += 4
                    else:
                        sizes.append(0)
                    if flags & 0x400:
                        off += 4
                    if flags & 0x800:
                        off += 4
            elif t2 == b"senc" or (t2 == b"uuid" and bytes(buf[b2 + 8: b2 + 24]) == PIFF_SENC):
                senc = (b2, p2)
            elif t2 in (b"saiz", b"saio"):
                _rename(buf, b2, b"free")
        crypto = tracks.get(track_id) or (next(iter(tracks.values())) if len(tracks) == 1 else None)
        if crypto is None:
            continue        # 未加密的軌
        if senc is None:
            raise CencError("fragment 缺少 senc（不支援以 saio 指向 mdat 的輔助資訊）")
        if crypto.key is None:
            raise CencError(f"沒有 KID {crypto.kid} 的金鑰")
        default_size = default_size or trex_defaults.get(track_id or 0, 0)
        sizes = [s or default_size for s in sizes]
        entries = _parse_senc(buf, senc[1], crypto.iv_size)
        if len(entries) != len(sizes):
            raise CencError(f"senc 樣本數（{len(entries)}）與 trun（{len(sizes)}）不符")
        pos = base + data_offset
        for size, (iv, subs) in zip(sizes, entries):
            iv = iv if crypto.iv_size else (crypto.constant_iv or b"")
            buf[pos: pos + size] = decrypt_sample(bytes(buf[pos: pos + size]), crypto, iv, subs)
            pos += size
        _rename(buf, senc[0], b"free")


def _read_box(f: BinaryIO) -> Optional[bytes]:
    header = f.read(8)
    if len(header) < 8:
        return None
    size, _ = struct.unpack(">I4s", header)
    if size == 1:
        ext = f.read(8)
        header += ext
        size = struct.unpack(">Q", ext)[0]
    elif size == 0:
        return header + f.read()
    return header + f.read(size - len(header))


def _trex_defaults(buf: bytearray, start: int, end: int) -> dict[int, int]:
    result = {}
    for typ, _, payload, box_end in _boxes(buf, start, end):
        if typ == b"mvex":
            for t2, _, p2, _ in _boxes(buf, payload, box_end):
                if t2 == b"trex":
                    tid = struct.unpack_from(">I", buf, p2 + 4)[0]
                    result[tid] = struct.unpack_from(">I", buf, p2 + 16)[0]
    return result


def decrypt_file(src: Path, dst: Path, keys: dict[str, str], report: Callable[[float], None] = lambda n: None) -> None:
    """keys：{kid hex: key hex}，"" 為未指定 KID 的金鑰；report(已處理位元組) 供顯示進度"""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        tracks: dict[int, TrackCrypto] = {}
        trex: dict[int, int] = {}
        pending: Optional[bytearray] = None     # 已讀的 moof，等待其 mdat
        while True:
            box = _read_box(fin)
            if box is None:
                break
            report(fin.tell())
            typ = box[4:8]
            if typ == b"moov":
                buf = bytearray(box)
                tracks = process_moov(buf, 8, len(buf))
                trex = _trex_defaults(buf, 8, len(buf))
                for t in tracks.values():
                    k = keys.get(t.kid) or keys.get("")
                    t.key = bytes.fromhex(k) if k else None
                fout.write(buf)
            elif typ == b"moof":
                if pending is not None:
                    fout.write(pending)
                pending = bytearray(box)
            elif typ == b"mdat" and pending is not None:
                moof_len = len(pending)
                pending += box
                if tracks:
                    process_fragment(pending, 0, moof_len, tracks, trex)
                fout.write(pending)
                pending = None
            else:
                if pending is not None:     # moof 與 mdat 之間的其他 box
                    pending += box
                else:
                    fout.write(box)
        if pending is not None:
            fout.write(pending)
        if not tracks:
            raise CencError("檔案中沒有加密的軌（找不到 encv / enca）")


# ---------------- 對外介面 ----------------

async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore")


async def decrypt_cenc(src: Path, dst: Path, key: str, kid: Optional[str] = None, ffmpeg: str = "ffmpeg",
                       label: str = "") -> bool:
    tmp = dst.with_name(dst.stem + ".decrypting" + dst.suffix)
    tool = shutil.which("mp4decrypt")
    try:
        if tool:
            code, out = await _run([tool, "--key", f"{kid}:{key}" if kid else f"1:{key}", str(src), str(tmp)])
            if code != 0:
                raise CencError(out[-600:])
        else:
            await run_in_thread(f"解密 {label or src.name}", src.stat().st_size, decrypt_file,
                                src, tmp, {**({kid: key} if kid else {}), "": key}, fmt=size)
    except (CencError, OSError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        await log.error(f"解密失敗：{e}")
        return False
    tmp.replace(dst)
    return True


async def looks_decodable(path: Path, ffmpeg: str = "ffmpeg", seconds: int = 3) -> Optional[bool]:
    """以 ffmpeg 試解前幾秒；AES-CTR 無法從密文判斷金鑰是否正確，錯誤金鑰只會得到雜訊。
    回傳 None 表示沒有 ffmpeg 無法檢查"""
    exe = find_ffmpeg(ffmpeg)
    if exe is None:
        return None
    code, out = await _run([exe, "-v", "error", "-t", str(seconds), "-i", str(path), "-f", "null", "-"])
    return code == 0 and not out.strip()
