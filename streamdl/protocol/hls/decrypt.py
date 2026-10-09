from pathlib import Path
from typing import Optional

from Crypto.Cipher import AES


def resolve_iv(iv_hex: Optional[str], media_sequence: Optional[int]) -> str:
    """RFC 8216 5.2：未提供 IV 時，以媒體序號（128-bit big-endian）作為 IV。
    舊專案固定使用全 0，在 MEDIA-SEQUENCE 不為 0 的串流會解密錯誤。"""
    if iv_hex:
        return iv_hex.lower().removeprefix("0x").zfill(32)
    return (media_sequence or 0).to_bytes(16, "big").hex()


_MP4_BOXES = {b"ftyp", b"styp", b"moof", b"moov", b"sidx", b"emsg", b"free", b"prft", b"mdat"}


def _media_signature(head: bytes) -> int:
    """解密後開頭的格式特徵：2 = 明確（MPEG-TS 連續同步位元組、fMP4 box），1 = 較弱（ID3、ADTS / MP3 frame sync），0 = 不明"""
    if len(head) >= 377 and head[0] == head[188] == head[376] == 0x47:
        return 2
    if len(head) >= 8 and head[4:8] in _MP4_BOXES:
        return 2
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) in (0xF0, 0xF2)):
        return 1
    return 0


def check_aes128(data: bytes, key: bytes, iv_hex: str) -> Optional[bool]:
    """以一個加密片段試解，判斷金鑰是否正確
    True：解出已知的片段格式；None：PKCS7 填充正確但格式不明（多半正確）；False：錯誤金鑰"""
    if len(data) < 32 or len(data) % 16:
        return False
    head = AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(data[:min(len(data), 1024)])
    sig = _media_signature(head)
    if sig == 2:
        return True
    last = AES.new(key, AES.MODE_CBC, data[-32:-16]).decrypt(data[-16:])
    pad = last[-1]
    if not (0 < pad <= 16 and last[-pad:] == bytes([pad]) * pad):
        return False
    return True if sig else None


def decrypt_aes128(src: Path, dst: Path, key: bytes, iv_hex: str) -> None:
    data = AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(src.read_bytes())
    pad = data[-1] if data else 0
    if 0 < pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        data = data[:-pad]
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dst)
