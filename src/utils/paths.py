import re
import html
from pathlib import Path
from urllib.parse import urlsplit

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """轉成 Windows 合法檔名；斜線換成外觀相近的字元以保留原意"""
    name = html.unescape(name or "")
    name = name.replace("\t", " ").replace("\n", " ").replace("/", "⧸").replace("\\", "⧹")
    name = _ILLEGAL.sub("_", name).strip().rstrip(" .")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    return name[:max_length] or "media"


def url_basename(url: str) -> str:
    """網址最後一段的檔名（不含 query）"""
    return Path(urlsplit(url).path).name


class StreamPaths:
    """單一串流的資料夾結構

    output_dir/
    ├── {title}.{ext}                最終檔案
    └── backup/{title}/
        ├── playlists/               原始播放清單備份
        ├── fragments/               原始片段、金鑰、init、media.m3u8
        ├── decrypted/               (decrypt 選項) 解密後的片段與 media.m3u8
        └── store.json               片段下載狀態，斷點續傳用
    """

    def __init__(self, output_dir: Path, title: str):
        self.title = sanitize_filename(title)
        self.output_dir = Path(output_dir)
        self.backup = self.output_dir / "backup" / self.title
        self.playlists = self.backup / "playlists"
        self.fragments = self.backup / "fragments"
        self.decrypted = self.backup / "decrypted"
        self.store = self.backup / "store.json"

    def final(self, ext: str) -> Path:
        return self.output_dir / f"{self.title}.{ext.lstrip('.')}"

    def ensure(self, decrypted: bool = False) -> "StreamPaths":
        for p in (self.playlists, self.fragments):
            p.mkdir(parents=True, exist_ok=True)
        if decrypted:
            self.decrypted.mkdir(parents=True, exist_ok=True)
        return self
