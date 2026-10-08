# PyInstaller 打包設定：pyinstaller StreamDownloader.spec
# ffmpeg 路徑：環境變數 FFMPEG_BIN，否則自動尋找；找不到則不內嵌（執行時改用 PATH 或 exe 同資料夾的 ffmpeg.exe）
import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


def find_ffmpeg():
    candidates = [os.environ.get("FFMPEG_BIN"),
                  r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe",
                  shutil.which("ffmpeg")]
    for c in candidates:
        # chocolatey 的 PATH 上是 shim（數百 KB），不能內嵌
        if c and Path(c).is_file() and Path(c).stat().st_size > 10_000_000:
            return c
    return None


ffmpeg = find_ffmpeg()
print(f"[spec] ffmpeg: {ffmpeg or '不內嵌'}")

a = Analysis(
    ["streamdl/__main__.py"],
    pathex=["."],
    binaries=[(ffmpeg, ".")] if ffmpeg else [],
    # 提取器以 pkgutil 動態載入、Fetcher / Protocol 為延遲匯入，必須明確收集
    hiddenimports=collect_submodules("streamdl") + collect_submodules("rich._unicode_data"),
    # 本專案不使用 GUI / 科學運算套件；環境中若有安裝會被間接拉入
    excludes=["tkinter", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib",
              "numpy", "pandas", "scipy", "IPython", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StreamDownloader",
    console=True,
    upx=False,
)
