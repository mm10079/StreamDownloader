"""--key：金鑰字串與金鑰檔案的各種寫法"""
import base64
import json

import pytest
from pydantic import ValidationError

from streamdl.core.keys import parse_key_text, parse_keys
from streamdl.core.options import Options

KID_V, KEY_V = "a1b2c3d4e5f60718293a4b5c6d7e8f90", "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
KID_A, KEY_A = "5e6f708192a3b4c5d6e7f8091a2b3c4d", "00112233445566778899aabbccddeeff"
UUID_V = f"{KID_V[:8]}-{KID_V[8:12]}-{KID_V[12:16]}-{KID_V[16:20]}-{KID_V[20:]}"


def b64(hex_: str, url: bool = False) -> str:
    raw = bytes.fromhex(hex_)
    return (base64.urlsafe_b64encode(raw).rstrip(b"=") if url else base64.b64encode(raw)).decode()


@pytest.mark.parametrize("text, expected", [
    (f"{KID_V}:{KEY_V}", [(KID_V, KEY_V)]),
    (KEY_V, [("", KEY_V)]),
    (f"{UUID_V.upper()}:0x{KEY_V.upper()}", [(KID_V, KEY_V)]),
    (f"--key {KID_V}:{KEY_V}", [(KID_V, KEY_V)]),
    (f"[CONTENT] {KID_V} : {KEY_V}", [(KID_V, KEY_V)]),
    (f"KID: {KID_V}  KEY: {KEY_V}", [(KID_V, KEY_V)]),
    (f"key_id={UUID_V} key={KEY_V}", [(KID_V, KEY_V)]),
    (f"key = {b64(KEY_V)}", [("", KEY_V)]),
    (f"{KID_V}:{KEY_V} {KID_A}:{KEY_A}", [(KID_V, KEY_V), (KID_A, KEY_A)]),
    (f"{KEY_V} {KEY_A}", [("", KEY_V), ("", KEY_A)]),                # 沒有 KID 標籤：兩組各自獨立
    ("pssh AAAAYHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAAEASEJMiqfPHjzk", []),   # 不是 16 bytes 的資料不誤判
    (f"{KEY_V}ff", []),                                                # 長度不符的 hex 不截斷誤用
])
def test_parse_line_formats(text, expected):
    assert parse_key_text(text) == expected


def test_clearkey_json():
    jwk = {"keys": [{"kty": "oct", "kid": b64(KID_V, url=True), "k": b64(KEY_V, url=True)},
                    {"kty": "oct", "kid": KID_A, "k": KEY_A}]}
    assert parse_key_text(json.dumps(jwk, indent=2)) == [(KID_V, KEY_V), (KID_A, KEY_A)]


def test_key_file_multiline_with_comments(tmp_path):
    f = tmp_path / "keys.txt"
    f.write_text(f"# 影像\n{KID_V}:{KEY_V}\n\n// 音訊\nAudio key -> {KID_A}:{KEY_A}\n說明文字沒有金鑰\n", encoding="utf-8")
    assert parse_keys(str(f)) == [(KID_V, KEY_V), (KID_A, KEY_A)]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_key_file_encodings(tmp_path, encoding):
    f = tmp_path / "keys.txt"
    f.write_text(f"{KID_V}:{KEY_V}\r\n{KEY_A}\r\n", encoding=encoding)
    assert parse_keys(str(f)) == [(KID_V, KEY_V), ("", KEY_A)]


def test_binary_key_file(tmp_path):
    f = tmp_path / "key_abc.key"
    f.write_bytes(bytes.fromhex(KEY_V))
    assert parse_keys(str(f)) == [("", KEY_V)]


def test_mix_file_and_inline_keys_and_dedupe(tmp_path):
    f = tmp_path / "我的 金鑰.txt"
    f.write_text(f"{KID_V}:{KEY_V}\n{KID_A}:{KEY_V}\n", encoding="utf-8")   # 兩個 KID 共用同一把金鑰也保留
    keys = parse_keys(f'"{f}", {KEY_A}, {KID_V}:{KEY_V}')
    assert keys == [(KID_V, KEY_V), (KID_A, KEY_V), ("", KEY_A)]


def test_unrecognized_input_fails_early(tmp_path):
    with pytest.raises(ValidationError, match="abc"):
        Options(key="abc")
    empty = tmp_path / "empty.txt"
    empty.write_text("沒有金鑰\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="金鑰檔"):
        Options(key=str(empty))


def test_options_normalizes_key(tmp_path):
    f = tmp_path / "keys.txt"
    f.write_text(f"KID: {UUID_V}  KEY: {KEY_V.upper()}\n{KEY_A}\n", encoding="utf-8")
    opts = Options(key=str(f))
    assert opts.key == f"{KID_V}:{KEY_V},{KEY_A}"
    assert opts.key_list == [(KID_V, KEY_V), ("", KEY_A)]
    assert opts.key_map == {KID_V: KEY_V, "": KEY_A}
    assert Options().key_list == []
