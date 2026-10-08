import os

import pytest

from streamdl.postprocess.cenc import CencError, decrypt_file

from .cenc_fixture import Scheme, encrypt_init, encrypt_segment, parse, synthetic_fmp4, walk


def make(tmp_path, scheme: Scheme, sizes=((300, 17, 64), (5, 1000))):
    samples = [[os.urandom(n) for n in frag] for frag in sizes]
    init, frags = synthetic_fmp4(samples)
    clear = init + b"".join(frags)
    enc = encrypt_init(init, scheme) + b"".join(encrypt_segment(f, scheme) for f in frags)
    src = tmp_path / "enc.mp4"
    src.write_bytes(enc)
    return samples, clear, src


def mdat_payloads(data: bytes) -> list[bytes]:
    return [b.data for b in parse(data) if b.type == b"mdat"]


def entry_types(data: bytes) -> set[bytes]:
    return {b.type for b in walk(parse(data)) if b.type in (b"avc1", b"encv", b"mp4a", b"enca", b"sinf", b"senc")}


@pytest.mark.parametrize("scheme", [
    Scheme("cenc", iv_size=8),
    Scheme("cenc", iv_size=16),
    Scheme("cbcs", iv_size=0, constant_iv=os.urandom(16), crypt=1, skip=9),
    Scheme("cbcs", iv_size=0, constant_iv=os.urandom(16), crypt=1, skip=0),
], ids=["cenc-iv8", "cenc-iv16", "cbcs-1:9", "cbcs-full"])
def test_roundtrip(tmp_path, scheme):
    samples, clear, src = make(tmp_path, scheme)
    assert mdat_payloads(src.read_bytes()) != mdat_payloads(clear)          # 確實有加密
    dst = tmp_path / "dec.mp4"
    decrypt_file(src, dst, {scheme.kid.hex(): scheme.key.hex()})
    out = dst.read_bytes()
    assert mdat_payloads(out) == mdat_payloads(clear)
    assert entry_types(out) == {b"avc1"}            # encv 改回 avc1，sinf / senc 已移除
    assert len(out) == len(src.read_bytes())        # 原地修改，不改變任何大小


def test_key_without_kid(tmp_path):
    s = Scheme("cenc")
    _, clear, src = make(tmp_path, s)
    decrypt_file(src, tmp_path / "dec.mp4", {"": s.key.hex()})
    assert mdat_payloads((tmp_path / "dec.mp4").read_bytes()) == mdat_payloads(clear)


def test_missing_key_and_unencrypted(tmp_path):
    s = Scheme("cenc")
    _, clear, src = make(tmp_path, s)
    with pytest.raises(CencError, match="沒有 KID"):
        decrypt_file(src, tmp_path / "dec.mp4", {"00" * 16: s.key.hex()})
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(clear)
    with pytest.raises(CencError, match="沒有加密的軌"):
        decrypt_file(plain, tmp_path / "dec2.mp4", {"": s.key.hex()})
