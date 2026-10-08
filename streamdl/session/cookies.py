"""Cookie 轉換工具。

所有 cookie 一律以 http.cookiejar.Cookie 保存完整屬性（domain / path / secure / expires），
避免舊版只複製 name=value 導致 cookie 被送到錯誤網域的問題。
"""
import urllib.request
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Iterable, Optional


def make_cookie(
    name: str,
    value: str,
    domain: str = "",
    path: str = "/",
    secure: bool = False,
    expires: Optional[int] = None,
    http_only: bool = False,
) -> Cookie:
    return Cookie(
        version=0, name=name, value=value,
        port=None, port_specified=False,
        domain=domain, domain_specified=bool(domain), domain_initial_dot=domain.startswith("."),
        path=path or "/", path_specified=True,
        secure=secure, expires=expires, discard=expires is None,
        comment=None, comment_url=None,
        rest={"HttpOnly": ""} if http_only else {},
    )


# ---------- 瀏覽器（Selenium 格式）----------

def from_browser(cookies: Iterable[dict]) -> list[Cookie]:
    result = []
    for c in cookies:
        expiry = c.get("expiry")
        result.append(make_cookie(
            name=c["name"], value=c["value"],
            domain=c.get("domain", ""), path=c.get("path", "/"),
            secure=bool(c.get("secure")), expires=int(expiry) if expiry else None,
            http_only=bool(c.get("httpOnly")),
        ))
    return result


def to_browser(jar: CookieJar) -> list[dict]:
    result = []
    for c in jar:
        item = {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path, "secure": c.secure}
        if c.expires:
            item["expiry"] = int(c.expires)
        result.append(item)
    return result


# ---------- Netscape cookies.txt ----------

def parse_netscape(text: str) -> list[Cookie]:
    result = []
    for line in text.splitlines():
        http_only = False
        if line.startswith("#HttpOnly_"):
            line, http_only = line[len("#HttpOnly_"):], True
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts
        result.append(make_cookie(
            name, value, domain=domain, path=path,
            secure=secure.upper() == "TRUE",
            expires=int(expires) if expires.isdigit() and int(expires) > 0 else None,
            http_only=http_only,
        ))
    return result


def to_netscape(jar: CookieJar) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        prefix = "#HttpOnly_" if c.has_nonstandard_attr("HttpOnly") else ""
        lines.append("\t".join([
            prefix + c.domain,
            "TRUE" if c.domain.startswith(".") else "FALSE",
            c.path,
            "TRUE" if c.secure else "FALSE",
            str(int(c.expires or 0)),
            c.name,
            c.value or "",
        ]))
    return "\n".join(lines) + "\n"


def parse_header_string(text: str, domain: str = "") -> list[Cookie]:
    """'a=1; b=2' 形式；沒有網域資訊，因此需要指定 domain"""
    result = []
    for pair in text.split(";"):
        if "=" in pair:
            name, value = pair.split("=", 1)
            result.append(make_cookie(name.strip(), value.strip(), domain=domain))
    return result


def load_user_cookies(source: str, default_domain: str = "") -> list[Cookie]:
    """使用者輸入的 --cookies：檔案路徑或字串"""
    if not source:
        return []
    path = Path(source)
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if "\t" in text:
            return parse_netscape(text)
        return parse_header_string(text.strip(), default_domain)
    return parse_header_string(source, default_domain)


def cookie_header(jar: CookieJar, url: str) -> str:
    """依 RFC 規則（網域 / 路徑 / secure / 過期）算出某網址該送的 Cookie 標頭"""
    req = urllib.request.Request(url)
    jar.add_cookie_header(req)
    return req.get_header("Cookie", "")
