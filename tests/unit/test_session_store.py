import asyncio

from src.core.store import Segment, SegmentStore, SegStatus
from src.session import Session
from src.session import cookies as ct


def test_cookie_header_respects_domain_and_path():
    s = Session()
    s.set_cookies([
        ct.make_cookie("a", "1", domain=".example.com"),
        ct.make_cookie("b", "2", domain="other.com"),
        ct.make_cookie("c", "3", domain=".example.com", path="/private"),
        ct.make_cookie("d", "4", domain=".example.com", secure=True),
    ])
    assert s.headers_for("http://cdn.example.com/x")["Cookie"] == "a=1"
    header = s.headers_for("https://cdn.example.com/private/x")["Cookie"]
    assert set(header.split("; ")) == {"a=1", "c=3", "d=4"}


def test_netscape_roundtrip():
    s = Session()
    s.set_cookies([ct.make_cookie("k", "v", domain=".x.com", secure=True, expires=2000000000, http_only=True)])
    parsed = ct.parse_netscape(s.export_netscape())
    assert len(parsed) == 1
    c = parsed[0]
    assert (c.name, c.value, c.domain, c.secure, c.expires) == ("k", "v", ".x.com", True, 2000000000)
    assert c.has_nonstandard_attr("HttpOnly")


def test_refresh_is_single_flight():
    async def main():
        s = Session()
        calls = 0

        async def refresher(_):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)

        s.set_refresher(refresher)
        seen = s.version
        results = await asyncio.gather(*(s.refresh(seen) for _ in range(20)))
        return calls, results, s.version

    calls, results, version = asyncio.run(main())
    assert calls == 1 and all(results) and version == 1


def test_fork_is_independent():
    s = Session(headers={"User-Agent": "UA"})
    s.set_cookies([ct.make_cookie("a", "1", domain=".x.com")])
    f = s.fork()
    f.set_cookies([ct.make_cookie("a", "2", domain=".x.com")])
    assert s.headers_for("https://x.com/")["Cookie"] == "a=1"
    assert f.headers_for("https://x.com/")["Cookie"] == "a=2"
    assert f.id != s.id and f.user_agent == "UA"


def test_store_roundtrip_and_resume(tmp_path):
    st = SegmentStore(tmp_path / "store.json")
    st.add(Segment(key=5, url="u5", filename="5.ts", status=SegStatus.DONE))
    st.add(Segment(key=3, url="u3", filename="3.ts", status=SegStatus.RUNNING))
    st.save()
    loaded = SegmentStore.load(tmp_path / "store.json")
    assert loaded.keys == [3, 5]
    assert loaded.get(3).status is SegStatus.PENDING          # 中斷的視為待下載
    assert [s.key for s in loaded.pending()] == [3]
    # 未完成的片段遇到新網址（簽章更新）會更新；已完成的不動
    loaded.add(Segment(key=3, url="u3-new", filename="3.ts"))
    loaded.add(Segment(key=5, url="u5-new", filename="5.ts"))
    assert loaded.get(3).url == "u3-new" and loaded.get(5).url == "u5"
