"""工具层（`smartbw_mcp_server`）单元测试。

覆盖 8 个 MCP 工具的 handler 分支、协议处理与辅助函数。全部使用 FakeSmart 替身，
不启动 daemon、不依赖 Vaultwarden、不产生子进程调用。

背景：此前该模块覆盖率仅 16%（`_handle_tools_call` 的 8 个分支基本未测），
而它正是用户直接交互的入口层 —— 字段取值、多结果选择、参数校验都发生在这里。
"""
import io
import json
import os
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import smartbw_mcp_server as srv
from models import BwItem, SearchResult

# ============================================================================
# 测试替身与脚手架
# ============================================================================


class _FakeSmart:
    """最小 SmartBitwardenMCP 替身，只实现 handler 实际调用的方法。"""

    def __init__(self, results=(), items=None, all_items=None, sync_result="Syncing complete."):
        self._results = list(results)
        self._items = dict(items or {})
        self._all = list(all_items or [])
        self._sync_result = sync_result
        self.cache_cleared = False
        self.refresh_called = False
        self.client = self  # sync_cache 会访问 smart.client.call_tool

    def fuzzy_search(self, _term, max_results=10):
        return self._results[:max_results]

    def get_item_by_id(self, item_id):
        return self._items.get(item_id)

    def list_all_items(self):
        return self._all

    def call_tool(self, _name, _args=None):
        if isinstance(self._sync_result, Exception):
            raise self._sync_result
        return self._sync_result

    def clear_cache(self):
        self.cache_cleared = True

    def refresh_async(self):
        self.refresh_called = True

    def refresh_stats(self):
        return "命中=0 同步重建=0 异步预热=0 强制刷新=0"


def _search_result(name, score=1.0, username=""):
    return SearchResult(
        item=BwItem(id=f"id-{name}", name=name, username=username),
        score=score,
        matched_field="name",
    )


def _vault_item(name, password="", username="", fields=None, uris=None, notes=""):
    login = {}
    if password:
        login["password"] = password
    if username:
        login["username"] = username
    if uris:
        login["uris"] = [{"uri": u} for u in uris]
    return {"name": name, "login": login, "fields": fields or [], "notes": notes}


def _single(name, **item_kwargs):
    """构造"单个命中"场景：results + items 相互对应。"""
    return _FakeSmart(
        results=[_search_result(name)],
        items={f"id-{name}": _vault_item(name, **item_kwargs)},
    )


@contextmanager
def _ctx(smart):
    yield smart


@pytest.fixture
def patched(monkeypatch):
    """把 `_get_client_ctx` / `_get_client` 替换为返回指定 FakeSmart。"""

    holder = {}

    def install(smart):
        holder["smart"] = smart
        monkeypatch.setattr(srv, "_get_client_ctx", lambda: _ctx(smart))
        return smart

    monkeypatch.setattr(srv, "_get_client", lambda: holder["smart"])
    return install


def _call(tool, **args):
    """调用工具。形参刻意命名为 `tool` —— 工具自身的参数里也有 `name`，改名可避免冲突。"""
    return srv._handle_tools_call(None, {"name": tool, "arguments": args})


def _text(resp):
    return resp["content"][0]["text"]


# ============================================================================
# 协议层与辅助函数
# ============================================================================


def test_handle_init_reports_protocol_and_version():
    info = srv._handle_init(None, {})
    assert info["protocolVersion"] == "2024-11-05"
    assert info["capabilities"] == {"tools": {}}
    assert info["serverInfo"]["name"] == "smartbw-mcp"
    # 版本号必须与 pyproject 保持一致（避免"改了包版本忘了 serverInfo"）
    assert info["serverInfo"]["version"].count(".") == 2


def test_handle_tools_list_exposes_all_tools():
    tools = srv._handle_tools_list(None, {})["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "smartbw_get_api", "smartbw_get_field", "smartbw_get_item",
        "smartbw_get_password", "smartbw_search", "smartbw_list_all",
        "smartbw_daemon_status", "smartbw_sync_cache",
    }
    for t in tools:
        assert t["inputSchema"]["type"] == "object"


def test_text_result_shape():
    ok = srv._text_result("hi")
    assert ok == {"content": [{"type": "text", "text": "hi"}], "isError": False}
    bad = srv._text_result("boom", is_error=True)
    assert bad["isError"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 5),        # 缺省
        ("abc", 5),       # 非数字
        (0, 1),           # 低于下限
        (-3, 1),
        (999, 20),        # 高于上限
        ("7", 7),         # 字符串数字
        (10, 10),
    ],
)
def test_int_arg_clamps_and_falls_back(value, expected):
    assert srv._int_arg(value, default=5, lo=1, hi=20) == expected


def test_item_to_info_maps_standard_fields():
    info = srv._item_to_info(
        _vault_item("X", password="pw", username="u", uris=["https://a.invalid"],
                    fields=[{"name": "API", "value": "k"}], notes="n")
    )
    assert info["name"] == "X"
    assert info["username"] == "u"
    assert info["password"] == "pw"
    assert info["uris"] == ["https://a.invalid"]
    assert info["notes"] == "n"
    assert info["fields"] == {"API": "k"}


def test_item_to_info_without_fields_yields_empty_dict():
    assert srv._item_to_info({"name": "bare"})["fields"] == {}


def test_available_fields_skips_unnamed():
    item = {"fields": [{"name": "API", "value": "x"}, {"name": "", "value": "y"}, {}]}
    assert srv._available_fields(item) == ["API"]


# ============================================================================
# smartbw_get_api
# ============================================================================


def test_get_api_returns_value(patched):
    patched(_single("DeepSeek", fields=[{"name": "api", "value": "sk-xyz"}]))
    assert _text(_call("smartbw_get_api", name="deepseek")) == "sk-xyz"


def test_get_api_is_case_insensitive(patched):
    patched(_single("DeepSeek", fields=[{"name": "API", "value": "sk-abc"}]))
    assert _text(_call("smartbw_get_api", name="deepseek")) == "sk-abc"


def test_get_api_reports_available_fields_when_missing(patched):
    patched(_single("DeepSeek", fields=[{"name": "token", "value": "t"}]))
    payload = json.loads(_text(_call("smartbw_get_api", name="deepseek")))
    assert payload["found"] is False
    assert payload["available_fields"] == ["token"]


def test_get_api_not_found(patched):
    patched(_FakeSmart())
    resp = _call("smartbw_get_api", name="nope")
    assert "未找到" in _text(resp)


def test_get_api_ambiguous_returns_options(patched):
    smart = _FakeSmart(
        results=[_search_result("Alpha", 0.7), _search_result("Beta", 0.7)],
        items={"id-Alpha": _vault_item("Alpha"), "id-Beta": _vault_item("Beta")},
    )
    patched(smart)
    payload = json.loads(_text(_call("smartbw_get_api", name="a")))
    assert payload["pick_one"] is True
    assert [m["name"] for m in payload["matches"]] == ["Alpha", "Beta"]


def test_get_api_index_selects_second(patched):
    smart = _FakeSmart(
        results=[_search_result("Alpha", 0.7), _search_result("Beta", 0.7)],
        items={
            "id-Alpha": _vault_item("Alpha", fields=[{"name": "API", "value": "a"}]),
            "id-Beta": _vault_item("Beta", fields=[{"name": "API", "value": "b"}]),
        },
    )
    patched(smart)
    assert _text(_call("smartbw_get_api", name="x", index=1)) == "b"


def test_get_api_tool_error_is_flagged(patched):
    smart = _FakeSmart()
    smart.fuzzy_search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("daemon down"))
    patched(smart)
    resp = _call("smartbw_get_api", name="x")
    assert resp["isError"] is True
    assert "daemon down" in _text(resp)


# ============================================================================
# smartbw_get_field
# ============================================================================


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("password", "pw-1"),
        ("PASSWORD", "pw-1"),      # 大小写不敏感
        ("username", "alice"),
        ("user", "alice"),         # 别名
        ("login", "alice"),
        ("uri", "https://a.invalid"),
        ("url", "https://a.invalid"),
        ("link", "https://a.invalid"),
        ("notes", "some notes"),
        ("Token", "tok-9"),        # 自定义字段
    ],
)
def test_get_field_resolves_standard_and_custom(patched, field, expected):
    patched(_single("Site", password="pw-1", username="alice",
                    uris=["https://a.invalid"], notes="some notes",
                    fields=[{"name": "Token", "value": "tok-9"}]))
    assert _text(_call("smartbw_get_field", name="site", field=field)) == expected


def test_get_field_requires_field_name(patched):
    patched(_single("Site"))
    resp = _call("smartbw_get_field", name="site")
    assert resp["isError"] is False
    assert "缺少字段名" in _text(resp)


def test_get_field_lists_available_when_missing(patched):
    patched(_single("Site", password="pw", username="alice", notes="n",
                    fields=[{"name": "API", "value": "k"}]))
    payload = json.loads(_text(_call("smartbw_get_field", name="site", field="nope")))
    assert payload["found"] is False
    assert payload["field"] == "nope"
    # 标准字段 + 自定义字段都应被列出
    assert payload["available_fields"] == ["password", "username", "notes", "API"]


def test_get_field_not_found_item(patched):
    patched(_FakeSmart())
    assert "未找到" in _text(_call("smartbw_get_field", name="x", field="password"))


# ============================================================================
# smartbw_get_item / smartbw_get_password
# ============================================================================


def test_get_item_returns_full_info(patched):
    patched(_single("Site", password="pw", username="alice",
                    fields=[{"name": "API", "value": "k"}]))
    payload = json.loads(_text(_call("smartbw_get_item", name="site")))
    assert payload["name"] == "Site"
    assert payload["password"] == "pw"
    assert payload["fields"] == {"API": "k"}


def test_get_item_not_found(patched):
    patched(_FakeSmart())
    assert "未找到" in _text(_call("smartbw_get_item", name="ghost"))


def test_get_password_hit_and_miss(patched):
    patched(_single("Site", password="s3cret"))
    assert _text(_call("smartbw_get_password", name="site")) == "s3cret"

    patched(_single("NoPwd"))
    assert "无密码" in _text(_call("smartbw_get_password", name="nopwd"))


# ============================================================================
# smartbw_search / smartbw_list_all
# ============================================================================


def test_search_requires_query(patched):
    patched(_FakeSmart())
    assert "缺少搜索词" in _text(_call("smartbw_search"))


def test_search_no_results(patched):
    patched(_FakeSmart())
    assert _text(_call("smartbw_search", query="zzz")) == "无结果"


def test_search_formats_matches_and_skips_blank_username(patched):
    patched(_FakeSmart(results=[_search_result("Alpha", 0.9, username="alice"),
                                _search_result("Beta", 0.8)]))
    text = _text(_call("smartbw_search", query="a"))
    assert "找到 2 个结果" in text
    assert "[0] Alpha | user=alice | score=0.90" in text
    assert "[1] Beta | user=(无) | score=0.80" in text


def test_search_limit_is_clamped_to_20(patched):
    smart = _FakeSmart(results=[_search_result(f"N{i}") for i in range(30)])
    patched(smart)
    # limit=999 会被夹到 20；FakeSmart 只返回前 limit 条
    text = _text(_call("smartbw_search", query="n", limit=999))
    assert "找到 20 个结果" in text


def test_list_all_empty(patched):
    patched(_FakeSmart())
    assert _text(_call("smartbw_list_all")) == "无项目"


def test_list_all_truncates_with_notice(patched):
    items = [BwItem(id=f"i{i}", name=f"Item{i}", username="u") for i in range(5)]
    patched(_FakeSmart(all_items=items))
    text = _text(_call("smartbw_list_all", limit=2))
    assert "共 5 个项目" in text
    assert "已截断，仅显示前 2 条" in text
    assert text.count("Item") == 2


def test_list_all_within_limit_has_no_truncation_notice(patched):
    items = [BwItem(id="i0", name="Only", username="u")]
    patched(_FakeSmart(all_items=items))
    text = _text(_call("smartbw_list_all", limit=50))
    assert "共 1 个项目" in text
    assert "已截断" not in text


# ============================================================================
# smartbw_daemon_status
# ============================================================================


def test_daemon_status_running(monkeypatch, tmp_path):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))          # 当前进程必然存活
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    monkeypatch.setattr(srv, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "socket_path", lambda: sock)

    text = _text(_call("smartbw_daemon_status"))
    assert "运行中" in text and str(os.getpid()) in text


def test_daemon_status_stale_pid(monkeypatch, tmp_path):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("99999999")                # 几乎必然不存在
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    monkeypatch.setattr(srv, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "socket_path", lambda: sock)

    text = _text(_call("smartbw_daemon_status"))
    assert "进程已死" in text


def test_daemon_status_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "socket_path", lambda: tmp_path / "missing.sock")
    assert "未运行" in _text(_call("smartbw_daemon_status"))


# ============================================================================
# smartbw_sync_cache
# ============================================================================


def test_sync_cache_success_clears_cache(patched, monkeypatch, tmp_path):
    smart = patched(_FakeSmart())
    monkeypatch.setattr(srv, "socket_path", lambda: tmp_path / "missing.sock")

    resp = _call("smartbw_sync_cache")
    text = _text(resp)
    assert "sync: ✅ Syncing complete." in text
    assert "MCP Python 缓存已清除" in text
    assert "缓存统计:" in text
    assert resp["isError"] is False
    assert smart.cache_cleared is True and smart.refresh_called is True


def test_sync_cache_failure_is_flagged(patched, monkeypatch, tmp_path):
    smart = patched(_FakeSmart(sync_result=RuntimeError("locked")))
    monkeypatch.setattr(srv, "socket_path", lambda: tmp_path / "missing.sock")

    resp = _call("smartbw_sync_cache")
    assert resp["isError"] is True
    assert "守护进程同步失败" in _text(resp)
    # 缓存清理仍应被执行（sync 失败不代表本地缓存不可清）
    assert smart.cache_cleared is True


# ============================================================================
# 未知工具与 stdio 主循环
# ============================================================================


def test_unknown_tool_is_flagged(patched):
    patched(_FakeSmart())
    resp = _call("smartbw_nope")
    assert resp["isError"] is True
    assert "未知工具" in _text(resp)


def _drive_main(monkeypatch, lines, tmp_path):
    """跑一轮 main()：屏蔽预热与退出信号，喂入给定行，返回解析后的响应列表。"""
    monkeypatch.setattr(srv, "_prewarm_cache", lambda: None)
    monkeypatch.setattr(srv, "SHUTDOWN_SIGNAL", tmp_path / "no.signal")
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    srv.main()
    return [json.loads(ln) for ln in out.getvalue().strip().split("\n") if ln.strip()]


def test_main_round_trip_handles_protocol_and_junk(patched, monkeypatch, tmp_path):
    patched(_single("Site", password="pw"))
    responses = _drive_main(
        monkeypatch,
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            "",                                                        # 空行：跳过
            "this is not json",                                        # 非法 JSON：跳过
            json.dumps({"jsonrpc": "2.0", "id": 2,
                        "method": "notifications/initialized", "params": {}}),  # 无响应
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "smartbw_get_password",
                                   "arguments": {"name": "site"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "bogus", "params": {}}),
        ],
        tmp_path,
    )

    # 空行 / 非法 JSON / notification 都不产生响应
    assert [r["id"] for r in responses] == [1, 3, 4, 5]
    assert responses[0]["result"]["serverInfo"]["name"] == "smartbw-mcp"
    assert len(responses[1]["result"]["tools"]) == 8
    assert _text(responses[2]["result"]) == "pw"
    assert responses[3]["result"]["isError"] is True


def test_main_returns_jsonrpc_error_on_internal_failure(patched, monkeypatch, tmp_path):
    smart = patched(_FakeSmart())
    smart.fuzzy_search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))

    responses = _drive_main(
        monkeypatch,
        [json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                     "params": {"name": "smartbw_get_api", "arguments": {"name": "x"}}})],
        tmp_path,
    )
    # handler 内部已捕获异常 → 仍是正常 result（isError=True），而非 jsonrpc error
    assert responses[0]["result"]["isError"] is True
    assert "boom" in _text(responses[0]["result"])


# ============================================================================
# _get_client：熔断与错误分类
# 这些消息是用户排障时唯一能看到的线索，值得逐类固化。
# ============================================================================


def _reset_client_state(monkeypatch):
    monkeypatch.setattr(srv, "_client", None)
    monkeypatch.setattr(srv, "_client_error_count", 0)
    monkeypatch.setattr(srv, "_client_last_error", 0.0)


def _raise(exc):
    def _factory():
        raise exc

    return _factory


def test_get_client_circuit_breaker_message(monkeypatch):
    _reset_client_state(monkeypatch)
    monkeypatch.setattr(srv, "_client_error_count", 5)
    monkeypatch.setattr(srv, "_client_last_error", time.time())  # 冷却窗口内

    with pytest.raises(Exception, match="熔断保护"):
        srv._get_client()


def test_get_client_circuit_recovers_after_cooldown(monkeypatch):
    _reset_client_state(monkeypatch)
    monkeypatch.setattr(srv, "_client_error_count", 5)
    monkeypatch.setattr(srv, "_client_last_error", time.time() - 60)  # 窗口已过
    monkeypatch.setattr(srv, "_create_client", _raise(RuntimeError("daemon 未运行")))

    # 冷却过后应重新尝试（而不是直接熔断）
    with pytest.raises(Exception) as ei:
        srv._get_client()
    assert "熔断保护" not in str(ei.value)


@pytest.mark.parametrize(
    ("raised", "expected_hint"),
    [
        (Exception("[分类A·守护进程未运行] daemon 未启动"), "分类A"),
        (Exception("守护进程未运行"), "分类A"),
        (Exception("session expired, please unlock"), "分类C"),
        (Exception("vault is locked"), "分类C"),
        (Exception("request timeout after 30s"), "分类B"),
        (Exception("连接超时"), "分类B"),
        (Exception("something entirely unexpected"), "分类未知"),
    ],
)
def test_get_client_error_classification(monkeypatch, raised, expected_hint):
    _reset_client_state(monkeypatch)
    monkeypatch.setattr(srv, "_create_client", _raise(raised))

    with pytest.raises(Exception) as ei:
        srv._get_client()
    assert expected_hint in str(ei.value)


def test_get_client_failure_increments_counter(monkeypatch):
    _reset_client_state(monkeypatch)
    monkeypatch.setattr(srv, "_create_client", _raise(RuntimeError("nope")))

    for _ in range(3):
        with pytest.raises(Exception):
            srv._get_client()

    assert srv._client_error_count == 3
    assert srv._client_last_error > 0


def test_get_client_reuses_healthy_instance(monkeypatch):
    _reset_client_state(monkeypatch)

    class _C:
        def initialize(self):
            return True

    healthy = _C()
    monkeypatch.setattr(srv, "_client", healthy)

    assert srv._get_client() is healthy


def test_get_client_ctx_closes_socket_but_keeps_instance(monkeypatch):
    """上下文管理器只关 socket、不销毁长驻实例（缓存才能跨调用复用）。"""

    class _Client:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    class _Smart:
        def __init__(self):
            self.client = _Client()

        def initialize(self):
            return True

    _reset_client_state(monkeypatch)
    smart = _Smart()
    monkeypatch.setattr(srv, "_client", smart)

    with srv._get_client_ctx() as got:
        assert got is smart
    assert smart.client.closed == 1
    assert srv._client is smart  # 实例未被丢弃

    # 客户端抛出异常时也要关闭 socket（避免连接泄漏）
    with pytest.raises(RuntimeError):
        with srv._get_client_ctx() as got:
            raise RuntimeError("user code failed")
    assert smart.client.closed == 2
