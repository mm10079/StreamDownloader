"""登入：優先以 httpx 走表單（快、可在無頭環境），失敗或沒有帳密時改用瀏覽器（可手動登入）"""
import asyncio
import time

from ....session import Session
from ....utils import log
from .pages import find_csrf
from .urls import ZanUrls

LOGIN_COOKIE = "nglives_pltk"
MANUAL_LOGIN_TIMEOUT = 600

_FILL_AND_SUBMIT = """
const [account, password, done] = [arguments[0], arguments[1], arguments[arguments.length - 1]];
const set = (sel, v) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  el.focus(); el.value = v;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
};
const ok = set("input[name='mailAddress']", account) && set("input[name='password']", password);
const btn = document.querySelector("input[name='__submit__'], input[type='submit'], button[type='submit']");
if (ok && btn) btn.click();
done(ok && !!btn);
"""


def logged_in(session: Session) -> bool:
    return any(c.name == LOGIN_COOKIE for c in session.jar)


async def login_httpx(session: Session, urls: ZanUrls, account: str, password: str) -> bool:
    if not account or not password:
        return False
    client = session.client()
    resp = await client.get(urls.login)
    csrf = find_csrf(resp.text)
    if not csrf:
        await log.warning("ZAN-LIVE：找不到登入表單的 CSRF token")
        return False
    await client.post(
        urls.login,
        data={"mailAddress": account, "password": password, "isPersistentLogin": "1",
              "_csrf": csrf, "__submit__": "登入"},
        headers={"Origin": urls.domain, "Referer": urls.login},
    )
    return logged_in(session)


def _browser_login_sync(browser, urls: ZanUrls, account: str, password: str) -> bool:
    browser.goto(urls.login)
    time.sleep(2)
    if account and password:
        try:
            browser.execute_async_script(_FILL_AND_SUBMIT, account, password)
        except Exception:
            pass
    deadline = time.time() + MANUAL_LOGIN_TIMEOUT
    while time.time() < deadline:
        try:
            if any(c.get("name") == LOGIN_COOKIE for c in browser.cookies):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


async def login_browser(session: Session, browser, urls: ZanUrls, account: str, password: str) -> bool:
    """以瀏覽器登入，成功後瀏覽器成為該 session 的 cookie 權威"""
    if not (account and password):
        await log.info(f"ZAN-LIVE：請在瀏覽器中手動登入（{MANUAL_LOGIN_TIMEOUT // 60} 分鐘內）")
    ok = await asyncio.to_thread(_browser_login_sync, browser, urls, account, password)
    if ok:
        await session.attach_browser(browser, authority=True)
    return ok
