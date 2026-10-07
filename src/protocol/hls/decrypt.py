from pathlib import Path
from typing import Optional

from Crypto.Cipher import AES


def resolve_iv(iv_hex: Optional[str], media_sequence: Optional[int]) -> str:
    """RFC 8216 5.2：未提供 IV 時，以媒體序號（128-bit big-endian）作為 IV。
    舊專案固定使用全 0，在 MEDIA-SEQUENCE 不為 0 的串流會解密錯誤。"""
    if iv_hex:
        return iv_hex.lower().removeprefix("0x").zfill(32)
    return (media_sequence or 0).to_bytes(16, "big").hex()


def decrypt_aes128(src: Path, dst: Path, key: bytes, iv_hex: str) -> None:
    data = AES.new(key, AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(src.read_bytes())
    pad = data[-1] if data else 0
    if 0 < pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        data = data[:-pad]
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dst)
