"""Chrome 設定檔工具

同一個設定檔資料夾同時只能由一個 Chrome 使用。需要多個瀏覽器時（例如多視角各自一個），
後續的瀏覽器改用「設定檔副本」：複製擴充功能、偏好設定與網站資料，略過快取與鎖定檔。
執行中的 Chrome 會鎖住部分檔案（例如 Cookies 資料庫），複製失敗的檔案直接略過，
登入狀態另外由 CDP 從原瀏覽器複製 cookies。
"""
import os
import shutil
import tempfile
import time
from pathlib import Path

# 快取與可重建的資料（小寫比對資料夾名稱，任何層級）
SKIP_DIRS = {
    "cache", "code cache", "gpucache", "dawncache", "dawngraphitecache", "dawnwebgpucache", "graphitedawncache",
    "shadercache", "grshadercache", "cachestorage", "media cache", "crashpad", "browsermetrics",
    "component_crx_cache", "extensions_crx_cache", "optimization_guide_model_store", "safe browsing",
    "segmentation_platform", "deferred browser metrics",
}
# 執行中 Chrome 的單一實例鎖與 LevelDB 鎖
SKIP_FILES = {"singletonlock", "singletoncookie", "singletonsocket", "lockfile", "lock"}


def default_profile_dir() -> str:
    """預設的固定設定檔位置"""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return str(Path(base) / "StreamDownloader" / "chrome")
    return str(Path.home() / ".streamdl" / "chrome")


def clone_profile(src: str) -> str:
    """把設定檔複製到新的暫存資料夾並回傳路徑；呼叫端負責在使用完畢後以 remove_profile 刪除"""
    source = Path(src)
    dest = Path(tempfile.mkdtemp(prefix="streamdl-chrome-"))
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        target = dest / Path(root).relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.lower() in SKIP_FILES:
                continue
            try:
                shutil.copy2(Path(root) / name, target / name)
            except OSError:
                pass        # 執行中的 Chrome 鎖住的檔案
    return str(dest)


def remove_profile(path: str, attempts: int = 10) -> None:
    """Chrome 結束後可能還會短暫占用檔案，重試幾次"""
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.5)
