import asyncio

from src.backfill import UrlDiffBackfill, build_template


def test_template_zero_padded_path():
    t = build_template("https://h/v/index_1/00000/index_1_01669.ts", "https://h/v/index_1/00000/index_1_01670.ts")
    assert t.pattern == "https://h/v/index_1/00000/index_1_{num}.ts"
    assert (t.fill, t.space) == (5, 1)
    assert t.format(7) == "https://h/v/index_1/00000/index_1_00007.ts"
    assert t.extract("https://h/v/index_1/00000/index_1_01700.ts") == 1700
    assert t.extract("https://h/v/other.ts") is None


def test_template_timestamp_and_query():
    t = build_template("https://h/1758184206626.ts?seg=1758184206626&sig=x", "https://h/1758184214111.ts?seg=1758184214111&sig=x")
    assert t.space == 7485 and t.fill == 0
    assert t.format(5) == "https://h/5.ts?seg=5&sig=x"


def test_template_rejects_independent_changes():
    # 序號與簽章同時改變 → 無法回溯
    assert build_template("https://h/1.ts?sig=111", "https://h/2.ts?sig=999") is None
    assert build_template("https://h/a.ts", "https://h/b.ts") is None


def test_braces_are_escaped():
    t = build_template("https://h/{x}/1.ts", "https://h/{x}/2.ts")
    assert t.format(3) == "https://h/{x}/3.ts"
    assert t.extract("https://h/{x}/9.ts") == 9


def _run(coro):
    return asyncio.run(coro)


def test_discover_continuous():
    valid = set(range(1234, 2000))
    calls = []

    async def probe(n):
        calls.append(n)
        return n in valid

    t = build_template("https://h/1900.ts", "https://h/1901.ts")
    found = _run(UrlDiffBackfill(distance=500).discover(t, [1900, 1901], probe))
    assert found == list(range(1234, 1900))
    assert len(calls) < 40      # 二分搜尋而非逐一探測


def test_discover_timestamp_with_learned_delta():
    valid = [10000, 18000, 26001, 33500, 41500]     # 33500 的間距 7501 不在常見清單 → 範圍掃描

    async def probe(n):
        return n in valid

    t = build_template("https://h/41500.ts", "https://h/49500.ts")
    assert t.space == 8000
    found = _run(UrlDiffBackfill(batch=500).discover(t, [41500, 49500], probe))
    assert found == [10000, 18000, 26001, 33500]
