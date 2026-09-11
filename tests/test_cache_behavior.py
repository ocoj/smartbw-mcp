"""缓存行为回归测试（离线，不依赖 daemon / Vaultwarden）。

覆盖 SMARTBW_CACHE_TTL 相关的全部策略：
命中复用、TTL 过期同步刷新、无结果 / 结果可疑重查、5s 门槛、
single-flight、无定时器、不返回任意陈旧数据、stdout 不被污染。

用 `_fetch_with_temp_client` 的替身计数，不需要真实后端。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CACHE_REFRESH_MIN_AGE  # noqa: E402
from smart_search import SmartBitwardenMCP  # noqa: E402


def _item(name):
    return {"id": f"id-{name}", "name": name, "login": {"username": "u"}}


class _FakeFetcher:
    """后端拉取替身：记录调用时刻，返回由 provider 控制的数据。"""

    def __init__(self, provider, delay=0.0):
        self._provider = provider
        self._delay = delay
        self.calls = []

    def __call__(self):
        if self._delay:
            time.sleep(self._delay)
        self.calls.append(time.time())
        return list(self._provider())

    @property
    def n(self):
        return len(self.calls)


def _make(provider, delay=0.0, ttl=None):
    smart = SmartBitwardenMCP(auto_init=False)
    if ttl is not None:
        smart._cache_ttl = ttl
    fetcher = _FakeFetcher(provider, delay)
    smart._fetch_with_temp_client = fetcher
    return smart, fetcher


def _age_cache(smart, seconds):
    """把缓存与最近一次装载尝试一起拨旧（门槛取两者的 max）。"""
    smart._cache_time = time.time() - seconds
    smart._last_attempt_time = time.time() - seconds


# ── 命中与 TTL ──────────────────────────────────────────────

def test_cold_start_loads_once():
    smart, f = _make(lambda: [_item("GitHub"), _item("GitLab")])
    results = smart.fuzzy_search("github")
    assert f.n == 1
    assert results and results[0].item.name == "GitHub"


def test_reuse_within_ttl():
    smart, f = _make(lambda: [_item("GitHub"), _item("GitLab")])
    smart.fuzzy_search("github")
    for _ in range(3):
        assert smart.fuzzy_search("gitlab")
    assert f.n == 1, "TTL 内应复用缓存，不得重新拉取"


def test_ttl_expiry_refreshes_synchronously():
    data = [_item("GitHub")]
    smart, f = _make(lambda: data, ttl=0.3)
    smart._ensure_items()
    assert f.n == 1

    data.append(_item("NewEntry"))
    time.sleep(0.4)
    items = smart._ensure_items()
    assert len(items) == 2, "过期时必须同步刷新，本次即拿到最新数据"
    assert f.n == 2

    smart._ensure_items()
    assert f.n == 2, "刷新后 TTL 内应命中"
    assert smart._name_index.get("newentry") is not None


# ── 补救式刷新（无结果 / 结果可疑）与门槛 ─────────────────────

def test_no_result_within_gate_skips_refresh():
    smart, f = _make(lambda: [_item("GitHub")])
    smart.fuzzy_search("github")
    n0 = f.n
    assert smart.search_items("zzz-nonexistent") == []
    assert f.n == n0, f"刚装载过（< {CACHE_REFRESH_MIN_AGE}s）不得重复拉取"


def test_no_result_after_gate_forces_refresh():
    data = [_item("GitHub")]
    smart, f = _make(lambda: data)
    smart.fuzzy_search("github")
    data.append(_item("Target-Alpha"))
    _age_cache(smart, 10)
    results = smart.search_items("Target-Alpha")
    assert len(results) == 1 and results[0].item.name == "Target-Alpha"


def test_suspicious_score_forces_refresh_and_keeps_better():
    # "abcdefg" vs "abcdxyz" -> SequenceMatcher ≈ 0.57，落在 [FUZZY_THRESHOLD, 可疑阈值) 区间
    data = [_item("abcdxyz"), _item("OtherThing")]
    smart, f = _make(lambda: data)
    smart.fuzzy_search("OtherThing")
    _age_cache(smart, 10)
    data.append(_item("abcdefg"))
    results = smart.search_items("abcdefg")
    assert results and results[0].item.name == "abcdefg", "刷新后应取更优结果"


def test_no_wasteful_refetch_right_after_load():
    """真机实测发现的缺陷：装载耗时与门槛同量级时，刚装载完立刻 miss 会白拉一遍。

    门槛必须按"装载完成时刻"计，因此这里故意让替身耗时 1s 再复现。
    """
    smart, f = _make(lambda: [_item("GitHub")], delay=1.0)
    smart.fuzzy_search("github")
    n0 = f.n
    assert smart.search_items("zzz-nonexistent") == []
    assert f.n == n0, "刚装载完不得立刻再拉一次"


# ── 并发与"无定时器" ────────────────────────────────────────

def test_single_flight_on_cold_start():
    smart, f = _make(lambda: [_item("GitHub")], delay=0.3)
    threads = [threading.Thread(target=smart._ensure_items) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert f.n == 1, "并发冷启动只允许一次真实拉取"


def test_idle_generates_zero_traffic():
    smart, f = _make(lambda: [_item("GitHub")], ttl=0.2)
    smart._ensure_items()
    n0 = f.n
    time.sleep(1.0)  # 早已超过 TTL，但无人查询
    assert f.n == n0, "没有定时器：空闲时不产生任何后端流量"


def test_long_idle_does_not_serve_stale():
    data = [_item("GitHub")]
    smart, f = _make(lambda: data)
    smart._ensure_items()
    data.append(_item("Later"))
    _age_cache(smart, 600)  # 模拟空闲 10 分钟
    items = smart._ensure_items()
    assert len(items) == 2, "空闲很久后的第一条查询必须返回最新数据，而非旧缓存"


# ── stdout 洁净度（stdio JSON-RPC 协议要求）───────────────────

def test_stdout_stays_clean(capsys):
    """无结果查询过去会 print 一行 emoji，污染 MCP 的 stdout 协议流。"""
    smart, _ = _make(lambda: [_item("GitHub")])
    smart.fuzzy_search("github")
    smart.search_items("zzz-nonexistent")
    _age_cache(smart, 10)
    smart.search_items("zzz-nonexistent")
    captured = capsys.readouterr()
    assert captured.out == "", f"库代码不得写 stdout：{captured.out!r}"
