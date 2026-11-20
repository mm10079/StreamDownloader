import asyncio
from pathlib import Path

from streamdl.core.options import Options
from streamdl.driver.browser.profile import clone_profile, remove_profile
from streamdl.extractor import ExtractContext
from streamdl.session import SessionManager


def test_clone_profile_skips_caches_and_locks(tmp_path):
    src = tmp_path / "profile"
    (src / "Default" / "Extensions" / "abc" / "1.0").mkdir(parents=True)
    (src / "Default" / "Extensions" / "abc" / "1.0" / "manifest.json").write_text("{}")
    (src / "Default" / "Local Extension Settings" / "abc").mkdir(parents=True)
    (src / "Default" / "Local Extension Settings" / "abc" / "000003.log").write_text("x")
    (src / "Default" / "Local Extension Settings" / "abc" / "LOCK").write_text("")
    (src / "Default" / "Cache" / "Cache_Data").mkdir(parents=True)
    (src / "Default" / "Cache" / "Cache_Data" / "data_0").write_text("big")
    (src / "Default" / "Preferences").write_text("{}")
    (src / "Local State").write_text("{}")
    (src / "SingletonLock").write_text("")

    dest = Path(clone_profile(str(src)))
    try:
        assert (dest / "Default" / "Extensions" / "abc" / "1.0" / "manifest.json").exists()
        assert (dest / "Default" / "Local Extension Settings" / "abc" / "000003.log").exists()
        assert (dest / "Default" / "Preferences").exists() and (dest / "Local State").exists()
        assert not (dest / "Default" / "Cache").exists()
        assert not (dest / "SingletonLock").exists()
        assert not (dest / "Default" / "Local Extension Settings" / "abc" / "LOCK").exists()
    finally:
        remove_profile(str(dest))
    assert not dest.exists()


def test_profile_option_default_and_none():
    assert Options().chrome_profile                      # 預設使用固定設定檔
    assert Options(chrome_profile="none").chrome_profile == ""
    assert Options(chrome_profile="").chrome_profile == ""


class FakeUC:
    instances: list = []

    def __init__(self):
        self.options = None
        self.user_data_dir = None
        self.temp_profile = False
        self.started = False
        self.imported = []
        self.closed = False
        FakeUC.instances.append(self)

    def start(self, headless=False):
        self.started = True

    def close(self):
        self.closed = True

    def export_cookies(self):
        return [{"name": "sl_session", "value": "ok", "domain": ".example", "path": "/"}]

    def import_cookies(self, cookies):
        self.imported += cookies
        return len(cookies)

    @property
    def current_url(self):
        return "about:blank"


def test_second_browser_uses_profile_copy_with_cookies(tmp_path, monkeypatch):
    import streamdl.driver as drv
    import streamdl.driver.browser.profile as prof
    monkeypatch.setattr(drv, "get_browser_class", lambda t: FakeUC)
    clones = []
    monkeypatch.setattr(prof, "clone_profile", lambda src: clones.append(src) or str(tmp_path / "copy"))
    FakeUC.instances = []
    ctx = ExtractContext(options=Options(chrome_profile=str(tmp_path / "p")), sessions=SessionManager())

    async def main():
        a = await ctx.new_browser(headless=False, profile=True)
        b = await ctx.new_browser(headless=False, profile=True)
        return a, b

    a, b = asyncio.run(main())
    assert a.user_data_dir == str((tmp_path / "p").resolve()) and not a.temp_profile
    assert b.user_data_dir == str(tmp_path / "copy") and b.temp_profile
    assert clones == [a.user_data_dir]
    assert [c["name"] for c in b.imported] == ["sl_session"]       # 登入狀態從原瀏覽器複製


def test_handoff_reused_only_for_profile_browser(tmp_path, monkeypatch):
    import streamdl.driver as drv
    monkeypatch.setattr(drv, "get_browser_class", lambda t: FakeUC)
    FakeUC.instances = []
    ctx = ExtractContext(options=Options(chrome_profile="none"), sessions=SessionManager())
    watch = FakeUC()
    ctx.browsers.append(watch)

    ctx.handoff = watch
    assert asyncio.run(ctx.new_browser(headless=False, profile=True)) is watch and ctx.handoff is None

    ctx.handoff = watch
    other = asyncio.run(ctx.new_browser(headless=False))           # 不需固定設定檔 → 關閉交接的瀏覽器
    assert other is not watch and watch.closed and watch not in ctx.browsers
