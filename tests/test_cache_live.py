"""真机集成测试：缓存策略 + MCP server 端到端（需要运行中的 daemon 与可达的 Vaultwarden）。

默认**跳过**，避免 CI / 日常 `pytest` 触碰真实密码库。显式启用：

    SMARTBW_LIVE_TEST=1 pytest tests/test_cache_live.py -v

仅执行读操作（sync / list / get），不写入密码库。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402  （需在 sys.path 注入之后导入）

pytestmark = [
    pytest.mark.slow,
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("SMARTBW_LIVE_TEST"),
        reason="需显式设置 SMARTBW_LIVE_TEST=1（会访问真实 Vaultwarden）",
    ),
]

SOCKET = paths.socket_path()


def _require_daemon():
    if not SOCKET.exists():
        pytest.skip(f"daemon 未运行（{SOCKET} 不存在）")


def test_live_cache_hit_expiry_and_no_timer():
    """真机计数后端拉取：命中复用 / 过期同步刷新 / 空闲零流量 / 门槛拦住重复拉取。"""
    _require_daemon()
    from config import CACHE_TTL
    from smart_search import SmartBitwardenMCP

    smart = SmartBitwardenMCP(use_daemon=True, timeout=60)
    assert smart.initialize()

    real_fetch = smart._fetch_with_temp_client
    calls = []

    def counted():
        t0 = time.time()
        result = real_fetch()
        calls.append(time.time() - t0)
        return result

    smart._fetch_with_temp_client = counted

    # 1) 冷启动必须拉一次
    assert smart.fuzzy_search("github")
    assert len(calls) == 1

    # 2) TTL 内应全部命中，且远快于一次拉取
    hit_times = []
    for query in ("github", "github", "deapsek"):
        t0 = time.time()
        smart.fuzzy_search(query)
        hit_times.append(time.time() - t0)
    assert len(calls) == 1, "TTL 内不得重新拉取"
    assert max(hit_times) < calls[0] / 2, f"命中应明显快于拉取：{hit_times} vs {calls[0]:.2f}s"

    # 3) 空闲（超过 TTL）不得产生流量 —— 没有定时器
    time.sleep(CACHE_TTL + 5)
    assert len(calls) == 1, "空闲时不得有任何后端流量"

    # 4) 过期后查询 -> 同步刷新
    smart.fuzzy_search("github")
    assert len(calls) == 2

    # 5) 紧接着无结果 -> 门槛应拦住，不得立刻再拉
    smart.search_items("zzz-nonexistent-xyz")
    assert len(calls) == 2, "刚装载完不得重复拉取"

    smart.close()


def test_live_stdio_server_end_to_end():
    """按真正的 MCP 协议起 server：版本、工具数、stdout 洁净度、sync_cache 输出。"""
    _require_daemon()
    server = ROOT / "smartbw_mcp_server.py"
    proc = subprocess.Popen(
        [sys.executable, str(server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        def send(obj, expect_id=None):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            if expect_id is None:
                return None
            while True:
                line = proc.stdout.readline()
                assert line, "MCP server 提前退出"
                line = line.strip()
                if not line:
                    continue
                # stdout 只允许 JSON-RPC；出现其它内容即为协议污染
                msg = json.loads(line)
                if msg.get("id") == expect_id:
                    return msg

        def call_tool(name, args, rid):
            resp = send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                         "params": {"name": name, "arguments": args}}, rid)
            return resp["result"]["content"][0]["text"]

        info = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, 1)
        assert info["result"]["serverInfo"]["name"] == "smartbw-mcp"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        assert len(send({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                         "params": {}}, 2)["result"]["tools"]) == 8

        time.sleep(7)  # 等启动预热完成
        assert "找到" in call_tool("smartbw_search", {"query": "github"}, 3)

        # 无结果查询：过去这里会把 emoji 写进 stdout（协议污染）
        assert "无结果" in call_tool("smartbw_search", {"query": "zzz-none"}, 4)

        out = call_tool("smartbw_sync_cache", {}, 5)
        assert "MCP Python 缓存已清除" in out
        assert "缓存统计" in out
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
