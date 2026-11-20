import os

import pytest

pytest.importorskip("undetected_chromedriver")     # 隨 streamdl[browser] 安裝

from streamdl.driver.browser import uc


@pytest.mark.skipif(os.name != "nt", reason="版本資料夾只存在於 Windows 的 Chrome 安裝目錄")
def test_version_from_install_folder(tmp_path, monkeypatch):
    """不依賴 PATH 上的 PowerShell：Chrome 旁的版本資料夾（更新後可能新舊並存，取最大）"""
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    for name in ("156.0.8000.1", "157.0.8103.79", "SetupMetrics", "chrome_proxy.exe"):
        (tmp_path / name).mkdir() if "." in name and name[0].isdigit() else (tmp_path / name).write_bytes(b"")
    monkeypatch.setenv("PATH", "")
    assert uc.chrome_major_version(str(exe)) == 157


@pytest.mark.skipif(os.name != "nt", reason="攜帶版的 .manifest 為 Windows 版 Chrome 的檔案")
def test_version_from_portable_manifest(tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    (tmp_path / "150.0.7871.24.manifest").write_text("")
    assert uc.chrome_major_version(str(exe)) == 150


def test_missing_binary_returns_none(tmp_path):
    assert uc.chrome_major_version(str(tmp_path / "nope.exe")) is None
