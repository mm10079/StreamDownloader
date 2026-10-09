"""--key 解析：金鑰字串或金鑰檔案 → [(kid, key)]（皆為 32 位小寫 hex，未指定 KID 時 kid 為 ""）

可辨識的寫法（檔案中每行一組，也可在同一行以逗號分隔）：
    KID:KEY、KEY                              a1b2c3d4...:0f1e2d3c...
    UUID 形式的 KID、0x 前綴                   a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90:0x0f1e2d3c...
    其他工具輸出的整行                         --key KID:KEY、[CONTENT] KID:KEY、KID: xxxx KEY: yyyy
    base64（需有 == 結尾）                    Dx4tPEtaaXiHlqW0w9Lh8A==
    ClearKey JSON（JWK，kid / k 為 base64url） {"keys": [{"kty": "oct", "kid": "...", "k": "..."}]}
    原始 16 bytes 二進位檔                     例如下載器產生的 .key 檔
"""
import base64
import binascii
import json
import re
from pathlib import Path

_HEX = r"(?:0x)?([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})"
_B64 = r"([A-Za-z0-9+/_-]{22}==)"
_TOKEN = re.compile(rf"(?<![0-9A-Za-z+/_-]){_HEX}(?![0-9a-fA-F-])|(?<![A-Za-z0-9+/_-]){_B64}")
_KID_LABEL = re.compile(r"\bkid\b|key[\s_-]?id", re.IGNORECASE)


def _from_b64(text: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == 16 else None


def _value(text: str) -> str | None:
    """單一欄位值（hex / UUID / base64）→ 32 位 hex"""
    text = text.strip()
    h = text.lower().removeprefix("0x").replace("-", "")
    if re.fullmatch(r"[0-9a-f]{32}", h):
        return h
    return _from_b64(text)


def _parse_line(line: str) -> list[tuple[str, str]]:
    tokens = []     # (起點, 終點, hex)
    for m in _TOKEN.finditer(line):
        value = m.group(1).replace("-", "").lower() if m.group(1) else _from_b64(m.group(2))
        if value:
            tokens.append((m.start(), m.end(), value))
    result, i = [], 0
    while i < len(tokens):
        # 以冒號相連的兩個值為 KID:KEY
        if i + 1 < len(tokens) and line[tokens[i][1]:tokens[i + 1][0]].strip() == ":":
            result.append((tokens[i][2], tokens[i + 1][2]))
            i += 2
            continue
        result.append(("", tokens[i][2]))
        i += 1
    # 「KID: xxx KEY: yyy」這類以標籤區分的寫法：同一行只有兩個值且前面標示 KID
    if len(result) == 2 and not result[0][0] and not result[1][0] and _KID_LABEL.search(line[:tokens[0][0]]):
        return [(result[0][1], result[1][1])]
    return result


def _parse_json(data) -> list[tuple[str, str]]:
    """JSON 中所有帶有 k / key 欄位的物件（ClearKey JWK 等）"""
    found = []
    if isinstance(data, dict):
        key = next((data[n] for n in ("k", "key") if isinstance(data.get(n), str)), None)
        kid = next((data[n] for n in ("kid", "keyid", "key_id", "keyId") if isinstance(data.get(n), str)), "")
        if key and _value(key):
            found.append((_value(kid) or "" if kid else "", _value(key)))
        for v in data.values():
            if isinstance(v, (dict, list)):
                found += _parse_json(v)
    elif isinstance(data, list):
        for v in data:
            found += _parse_json(v)
    return found


def parse_key_text(text: str) -> list[tuple[str, str]]:
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return _parse_json(json.loads(stripped))
        except ValueError:
            pass
    found = []
    for line in stripped.splitlines():
        if line.lstrip().startswith(("#", "//", ";")):
            continue        # 註解
        found += _parse_line(line)
    return found


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def load_key_file(path: Path) -> list[tuple[str, str]]:
    data = path.read_bytes()
    found = parse_key_text(_decode(data))
    if not found and len(data) == 16:      # 原始二進位金鑰
        found = [("", data.hex())]
    return found


def parse_keys(value: str) -> list[tuple[str, str]]:
    """--key 的值：以逗號分隔的金鑰或金鑰檔路徑；同一組 (kid, key) 只留一次。無法辨識的項目拋出 ValueError"""
    result: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip().strip('"').strip()
        if not item:
            continue
        path = Path(item).expanduser()
        if len(item) < 260 and path.is_file():
            found = load_key_file(path)
            where = f"金鑰檔 {path}"
        else:
            found = parse_key_text(item)
            where = f"「{item}」"
        if not found:
            raise ValueError(f"--key 無法從{where}取得金鑰（需為 32 位 hex 的 KEY 或 KID:KEY，或 base64）")
        result += [k for k in found if k not in result]
    return result
