"""审计修复（AUDIT_REPORT_v2.3.1 R3 / v2.3.2）回归测试。

全部离线、不依赖真实 Vaultwarden / daemon / bw CLI，且不触碰真实配置目录
（统一用 monkeypatch 把 SMARTBW_CONFIG_DIR 指向 tmp_path）。

覆盖：
- P0-2 / P1-1  统一路径语义（自定义目录读写一致、`~` 展开）
- P0-3         api_key 纳入加密
- P1-2         密文不可降级为明文
- P1-7         `_send_raw` 非阻塞分帧（半行不挂死、无数据按超时返回）
- P1-8 / P1-9  list_all limit 夹取、`_is_unlocked` 白名单
- P1-10        熔断器计入通用异常
- P1-11        PID 归属校验
- P1-13        `_resolve_search` 的多结果/自动选取/index 语义
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================================
# P0-3 / P1-2：加解密与密文降级防护
# ============================================================================

def test_crypto_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path))
    import crypto_config

    secret = "s3cr3t-value"
    enc = crypto_config.encrypt_value(secret)
    assert enc.startswith(crypto_config.ENCRYPTED_PREFIX)
    assert crypto_config.decrypt_value(enc) == secret
    # 非密文 → None；密文损坏 → None（不得抛异常）
    assert crypto_config.decrypt_value("plain-text") is None
    assert crypto_config.decrypt_value(crypto_config.ENCRYPTED_PREFIX + "bad") is None
    assert crypto_config.encrypt_value("") == ""


def test_api_key_is_encrypted_and_restored(monkeypatch, tmp_path):
    """P0-3：api_key 内含 clientSecret，必须与 master_password 同等加密。"""
    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path))
    import crypto_config

    api_key = "user.aaaaaaaa-bbbb-cccc.dddddddddddddddddddddddddddddddd"
    (tmp_path / "config.json").write_text(json.dumps({"api_key": api_key}))

    result = crypto_config.process_config_on_startup()
    assert result["api_key"] == api_key, "内存中应还原为明文"

    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["api_key"].startswith(crypto_config.ENCRYPTED_PREFIX), \
        "落盘必须是密文"


def test_ciphertext_never_used_as_plaintext(monkeypatch, tmp_path):
    """P1-2：解密失败时，密文不得原样进入 config（否则会被当成主密码去登录）。"""
    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_SERVER_PATH", "/nonexistent-mcp-server")  # 跳过路径自动发现
    monkeypatch.delenv("BW_MASTER_PASSWORD", raising=False)
    import config as config_mod

    (tmp_path / "config.json").write_text(json.dumps({
        "master_password": "!enc:v1:gAAAAABinvalidtokenAAAA",
        "client_secret": "",
    }))
    monkeypatch.setattr(config_mod, "_config_cache", None)

    cfg = config_mod.get_config(refresh=True)
    assert not cfg["master_password"].startswith("!enc:v1:"), \
        "密文不得降级为明文使用"


def test_runtime_dir_supports_tilde_and_custom(monkeypatch, tmp_path):
    """P1-1：SMARTBW_CONFIG_DIR 支持 ~ 展开；未设置时回落到默认目录。"""
    import config as config_mod

    monkeypatch.setenv("SMARTBW_CONFIG_DIR", "~/some-fake-dir")
    resolved = config_mod._get_runtime_dir()
    assert "~" not in str(resolved)
    assert str(resolved).startswith(str(Path.home()))

    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path))
    assert config_mod._get_runtime_dir() == tmp_path

    monkeypatch.delenv("SMARTBW_CONFIG_DIR", raising=False)
    assert config_mod._get_runtime_dir() == Path.home() / ".config" / "bitwarden-mcp"


# ============================================================================
# P1-9：_is_unlocked 白名单
# ============================================================================

def _fake_status(stdout: str):
    def _run(*_args, **_kwargs):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    return _run


@pytest.mark.parametrize("stdout,expected", [
    ('{"status":"unlocked"}', True),
    ('{"status":"locked"}', True),
    ('{"status":"unauthenticated"}', False),
    ("", False),                      # 空输出：过去会被误判为已登录
    ("some unexpected error text", False),
    ("You are not logged in.", False),
])
def test_is_unlocked_whitelist(monkeypatch, stdout, expected):
    monkeypatch.setattr(subprocess, "run", _fake_status(stdout))
    from unlock import _is_unlocked
    assert _is_unlocked({}) is expected


# ============================================================================
# P1-10：熔断器
# ============================================================================

def _bare_client():
    from mcp_raw import RealMCPClient
    client = RealMCPClient.__new__(RealMCPClient)
    client._failure_count = 0
    client._last_failure_time = 0.0
    client._circuit_open_until = 0.0
    client._max_failures = 5
    client._circuit_cooldown = 30.0
    return client


def test_circuit_counts_generic_exception():
    """通用 Exception（服务端业务错误）也必须计入熔断。"""
    client = _bare_client()

    def boom():
        raise Exception("MCP 错误 [-32603]: something bad")

    with pytest.raises(Exception):
        client._with_circuit(boom)
    assert client._failure_count == 1


def test_circuit_opens_after_max_failures():
    client = _bare_client()

    def boom():
        raise RuntimeError("boom")

    for _ in range(client._max_failures):
        with pytest.raises(RuntimeError):
            client._with_circuit(boom)
    assert client._is_circuit_open() is True

    from models import LockedError
    with pytest.raises(LockedError):
        client._with_circuit(lambda: "never runs")


# ============================================================================
# P1-11：PID 归属校验
# ============================================================================

def test_pid_is_daemon_rejects_foreign_pid():
    from mcp_daemon import _pid_is_daemon
    # 当前进程是 pytest，cmdline 不含 mcp_daemon
    assert _pid_is_daemon(os.getpid()) is False
    # 不存在的 PID：无法校验 → None（或 False）
    assert _pid_is_daemon(999999) in (None, False)


# ============================================================================
# P1-8 / P1-13：参数夹取与 _resolve_search
# ============================================================================

def test_int_arg_clamps_and_falls_back():
    from smartbw_mcp_server import _int_arg
    assert _int_arg(50, default=5, lo=1, hi=20) == 20
    assert _int_arg(0, default=5, lo=1, hi=20) == 1
    assert _int_arg(None, default=5, lo=1, hi=20) == 5
    assert _int_arg("abc", default=5, lo=1, hi=20) == 5
    assert _int_arg("7", default=5, lo=1, hi=20) == 7


class _FakeSmart:
    def __init__(self, names, items):
        from models import BwItem, SearchResult
        self._results = [SearchResult(item=BwItem(id=f"id-{n}", name=n), score=s,
                                      matched_field="name") for n, s in names]
        self._items = {f"id-{n}": items[n] for n in (n for n, _ in names)}

    def fuzzy_search(self, _term, max_results=10):
        return self._results[:max_results]

    def get_item_by_id(self, item_id):
        return self._items.get(item_id)


def test_resolve_search_no_result():
    from smartbw_mcp_server import _resolve_search
    item, err = _resolve_search(_FakeSmart([], {}), "nope")
    assert item is None and "未找到" in err


def test_resolve_search_single_result():
    from smartbw_mcp_server import _resolve_search
    items = {"Alpha": {"name": "Alpha", "login": {"password": "p"}}}
    item, err = _resolve_search(_FakeSmart([("Alpha", 1.0)], items), "alpha")
    assert err is None and item["name"] == "Alpha"


def test_resolve_search_high_confidence_autopick():
    from smartbw_mcp_server import _resolve_search
    items = {"Alpha": {"name": "Alpha"}, "Alphabet": {"name": "Alphabet"}}
    item, err = _resolve_search(_FakeSmart([("Alpha", 1.0), ("Alphabet", 0.6)], items), "alpha")
    assert err is None and item["name"] == "Alpha", "首结果 ≥0.95 且领先 ≥0.2 应自动选取"


def test_resolve_search_ambiguous_returns_options():
    from smartbw_mcp_server import _resolve_search
    items = {"Alpha": {"name": "Alpha"}, "Alpine": {"name": "Alpine"}}
    item, err = _resolve_search(_FakeSmart([("Alpha", 0.8), ("Alpine", 0.75)], items), "alp")
    assert item is None
    payload = json.loads(err)
    assert payload["pick_one"] is True and len(payload["matches"]) == 2


def test_resolve_search_index_selects():
    from smartbw_mcp_server import _resolve_search
    items = {"Alpha": {"name": "Alpha"}, "Alpine": {"name": "Alpine"}}
    item, err = _resolve_search(_FakeSmart([("Alpha", 0.8), ("Alpine", 0.75)], items), "alp", index=1)
    assert err is None and item["name"] == "Alpine"


# ============================================================================
# P1-7：_send_raw 非阻塞分帧
# ============================================================================

_CHILD_PARTIAL_LINE = r"""
import sys, time
sys.stdin.buffer.readline()
out = sys.stdout.buffer
out.write(b'{"jsonrpc":"2.0","id":1,"res')
out.flush()
time.sleep(0.3)
out.write(b'ult":{"ok":true}}' + b"\n")
out.flush()
time.sleep(5)
"""

_CHILD_SILENT = r"""
import sys, time
sys.stdin.buffer.readline()
time.sleep(30)
"""


def _manager_with_child(script):
    from mcp_daemon import MCPServerManager
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    mgr = object.__new__(MCPServerManager)
    mgr.process = proc
    mgr._lock = threading.Lock()
    mgr._request_id = 0
    return mgr, proc


def test_send_raw_frames_partial_lines():
    """子进程把一行拆成两次 write：必须按 \\n 分帧拼回，不得卡在 readline。"""
    mgr, proc = _manager_with_child(_CHILD_PARTIAL_LINE)
    try:
        resp = mgr._send_raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5)
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    finally:
        proc.kill()
        proc.wait()


def test_send_raw_times_out_instead_of_hanging():
    """子进程永不出行：必须在超时后抛 BwTimeoutError，而不是永久阻塞（P1-7）。"""
    from models import BwTimeoutError
    mgr, proc = _manager_with_child(_CHILD_SILENT)
    t0 = time.time()
    try:
        with pytest.raises(BwTimeoutError):
            mgr._send_raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=0.6)
        assert time.time() - t0 < 5, "不得阻塞在管道读取上"
    finally:
        proc.kill()
        proc.wait()


# ============================================================================
# P2-12：NEEDS_REINIT 标记的跨目录干扰
# ============================================================================

def test_reinit_marker_under_isolated_home():
    """防回归：测试期间 HOME 必须被隔离，标记不得落到真实 ~/.smartbw-mcp/。

    （曾因只隔离 SMARTBW_CONFIG_DIR 而漏掉运行目录，导致本套用例把 NEEDS_REINIT
    写进了用户真实环境。）

    注意：**真机模式（SMARTBW_LIVE_TEST=1）也必须成立** —— 此前该模式下整体不隔离、
    且本用例自行 skip，等于保护与守护同时失效。现在只把 socket 指回真实路径，
    HOME 仍然隔离，故本断言无条件执行。
    """
    import crypto_config
    assert "smartbw-test-home-" in str(crypto_config.REINIT_FILE)


def test_socket_path_default_and_override(monkeypatch):
    """socket 路径的单一来源：默认落在运行状态目录，`SMARTBW_SOCKET_PATH` 可覆盖。"""
    import paths

    monkeypatch.delenv("SMARTBW_SOCKET_PATH", raising=False)
    assert paths.socket_path() == paths.state_dir() / "daemon.sock"

    monkeypatch.setenv("SMARTBW_SOCKET_PATH", "~/real/daemon.sock")
    assert paths.socket_path() == Path("~/real/daemon.sock").expanduser()
    # 覆盖 socket 不得影响运行状态目录本身（仅放行这一个路径）
    assert paths.state_dir() == Path.home() / ".smartbw-mcp"


def test_state_dir_is_independent_of_config_dir(monkeypatch, tmp_path):
    """配置目录与运行状态目录是两个概念：改 SMARTBW_CONFIG_DIR 不得移动运行目录。"""
    import paths

    # 真机模式下 conftest 会设置 socket 覆盖，这里只测"默认推导"，故先清掉
    monkeypatch.delenv("SMARTBW_SOCKET_PATH", raising=False)
    before = paths.state_dir()
    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path / "cfg"))
    assert paths.runtime_dir() == tmp_path / "cfg"
    assert paths.state_dir() == before
    assert paths.socket_path() == before / "daemon.sock"


def test_daemon_module_paths_share_one_source():
    """daemon / server 的运行状态路径必须同源于 paths（曾各自硬编码，共 5 处）。"""
    import mcp_daemon
    import paths
    import smartbw_mcp_server

    assert mcp_daemon.SOCKET_PATH == paths.socket_path()
    assert mcp_daemon.PID_FILE.parent == paths.state_dir()
    assert mcp_daemon.LOG_FILE.parent == paths.state_dir()
    assert smartbw_mcp_server.SHUTDOWN_SIGNAL.parent == paths.state_dir()


def test_reinit_marker_not_deleted_for_other_config_dir(monkeypatch, tmp_path):
    """P2-12：用 SMARTBW_CONFIG_DIR 指向别的目录时，不得删除其它目录的标记。"""
    import crypto_config

    marker = crypto_config.REINIT_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("修复: 编辑 /some/other/config.json")

    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"master_password": "plain-secret"}))

    crypto_config.process_config_on_startup()
    assert marker.exists(), "不得删除属于其它配置目录的 NEEDS_REINIT"
    marker.unlink()


def test_reinit_marker_cleared_when_it_belongs_to_us(monkeypatch, tmp_path):
    """P2-12 反向：标记内容指向本配置目录时，成功启动后应被清理。"""
    import crypto_config

    marker = crypto_config.REINIT_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"修复: 编辑 {tmp_path / 'config.json'}")

    monkeypatch.setenv("SMARTBW_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"master_password": "plain-secret"}))

    crypto_config.process_config_on_startup()
    assert not marker.exists()


# ============================================================================
# P1-4 残留缺口：日志轮转后新建文件必须保持 0o600
# （由第三方复核报告 AUDIT_REVIEW_v2.3.2.md §2 提出）
# ============================================================================

def test_rotated_log_file_stays_private(tmp_path):
    """轮转后新建的当前日志必须仍是 0o600，不得回落 umask（如 0o664）。"""
    import mcp_daemon

    log = tmp_path / "daemon.log"
    handler = mcp_daemon._PrivateTimedRotatingFileHandler(
        str(log), when="S", interval=1, backupCount=1
    )
    lg = logging.getLogger("rotate-test")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        lg.info("before rollover")
        handler.doRollover()
        lg.info("after rollover")

        assert (log.stat().st_mode & 0o777) == 0o600, \
            f"轮转后当前日志权限应为 0o600，实际 {oct(log.stat().st_mode & 0o777)}"

        backups = [p for p in tmp_path.iterdir() if p != log]
        assert backups, "轮转应产生 backup 文件"
        for b in backups:
            assert (b.stat().st_mode & 0o777) == 0o600, \
                f"backup {b.name} 权限应为 0o600，实际 {oct(b.stat().st_mode & 0o777)}"
    finally:
        handler.close()
        lg.removeHandler(handler)


# ============================================================================
# daemon 双实例竞态（重启窗口）
#
# 实测事故：systemd 重启 daemon 的窗口内，客户端连接失败后盲目 Popen 拉起新实例，
# 而 DaemonServer.start() 无条件 unlink+bind ⇒ 两个进程同时 LISTEN 同一 socket、
# PID 文件被野实例抢占、systemd 管的那只变成谁都连不上的孤儿。
# 三层防护：bind 前探测 / 启动排他锁 / 客户端宽限等待。
# ============================================================================

def test_socket_is_live_distinguishes_listener_from_stale(tmp_path):
    """`_socket_is_live` 必须区分"有监听者"与"陈旧文件"。"""
    import socket as _socket

    import mcp_daemon

    assert mcp_daemon._socket_is_live(tmp_path / "missing.sock") is False

    stale = tmp_path / "stale.sock"
    stale.write_text("")
    assert mcp_daemon._socket_is_live(stale) is False

    live = tmp_path / "live.sock"
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        srv.bind(str(live))
        srv.listen(1)
        assert mcp_daemon._socket_is_live(live) is True
    finally:
        srv.close()
    # 监听者关闭后，文件仍在但已无监听 ⇒ 属于可安全清理的陈旧 socket
    assert live.exists()
    assert mcp_daemon._socket_is_live(live) is False


def test_socket_is_live_treats_backlog_full_as_alive(tmp_path):
    """连接队列满（EAGAIN）时必须保守判为"活着"。

    否则调用方会 unlink + bind，把正在运行实例的 socket 抢走 —— 即 v2.3.6 要修的
    那种分裂状态（实测：backlog 满时探测 errno=11，收窄前的实现返回 False）。
    """
    import socket as _socket

    import mcp_daemon

    sock = tmp_path / "busy.sock"
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    queued = []
    try:
        srv.bind(str(sock))
        srv.listen(1)  # 极小 backlog，便于塞满未 accept 的连接队列
        for _ in range(6):
            c = _socket.socket(_socket.AF_UNIX)
            c.setblocking(False)
            try:
                c.connect(str(sock))
                queued.append(c)
            except OSError:
                pass
        assert mcp_daemon._socket_is_live(sock) is True, (
            "队列满(EAGAIN)被误判为陈旧，会导致 unlink+bind 抢占活实例 socket"
        )
    finally:
        for c in queued:
            c.close()
        srv.close()


def test_daemon_lock_is_exclusive(monkeypatch, tmp_path):
    """启动排他锁：同一时刻只有一个持有者（第二次获取必须失败）。"""
    import mcp_daemon

    if mcp_daemon.fcntl is None:  # pragma: no cover - 非 Unix
        pytest.skip("平台无 fcntl")

    lock = tmp_path / "daemon.lock"
    monkeypatch.setattr(mcp_daemon, "LOCK_FILE", lock)
    monkeypatch.setattr(mcp_daemon, "_DAEMON_LOCK_FD", None)
    try:
        assert mcp_daemon._acquire_daemon_lock() is True
        assert (lock.stat().st_mode & 0o777) == 0o600, "锁文件权限应为 0o600"
        # 同一进程另开 fd 再锁同样应被拒（flock 按 open file description 计）
        assert mcp_daemon._acquire_daemon_lock() is False
        mcp_daemon._release_daemon_lock()
        assert mcp_daemon._acquire_daemon_lock() is True
    finally:
        mcp_daemon._release_daemon_lock()


def test_daemon_server_aborts_instead_of_hijacking_socket(monkeypatch, tmp_path):
    """已有实例在监听时，新实例必须放弃启动，且**不得**抢走 socket 或写 PID。"""
    import socket as _socket

    import mcp_daemon

    class _DummyMCP:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return True

        def stop(self):
            pass

    sock_path = tmp_path / "daemon.sock"
    pid_file = tmp_path / "daemon.pid"
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    try:
        monkeypatch.setattr(mcp_daemon, "SOCKET_PATH", sock_path)
        monkeypatch.setattr(mcp_daemon, "PID_FILE", pid_file)
        monkeypatch.setattr(mcp_daemon, "MCPServerManager", _DummyMCP)

        server = mcp_daemon.DaemonServer("dummy-session")
        assert server.start() is False, "应主动放弃启动"

        assert sock_path.exists(), "不得 unlink 别人的 socket"
        assert mcp_daemon._socket_is_live(sock_path) is True, "原监听者必须仍然可用"
        assert not pid_file.exists(), "放弃启动时不得留下 PID 文件"
    finally:
        srv.close()


class _UnconnectableDaemonClient:
    """`connect()` 永远失败的假 daemon 客户端。"""

    def __init__(self):
        self.connect_calls = 0

    def is_connected(self):
        return False

    def connect(self):
        self.connect_calls += 1
        return False


def _make_bare_client():
    """构造不带真实依赖的 RealMCPClient（跳过会取配置/建连接的 __init__）。"""
    import mcp_raw

    client = mcp_raw.RealMCPClient.__new__(mcp_raw.RealMCPClient)
    client._daemon_client = _UnconnectableDaemonClient()
    return client


def test_client_does_not_spawn_when_daemon_process_exists(monkeypatch):
    """PID 文件指向活着的 daemon 时（哪怕 socket 未就绪），客户端只能等，不得再拉起。"""
    import mcp_raw

    monkeypatch.setattr(mcp_raw, "DAEMON_WAIT_SECONDS", 0)
    monkeypatch.setattr(mcp_raw.RealMCPClient, "_daemon_process_exists",
                        staticmethod(lambda: True))
    monkeypatch.setattr(mcp_raw.time, "sleep", lambda *_: None)
    spawned = []
    monkeypatch.setattr(mcp_raw.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or None)

    assert _make_bare_client().start() is False
    assert spawned == [], "已有 daemon 进程时不得 spawn 第二个实例"


def test_client_spawns_when_no_daemon_process(monkeypatch):
    """确认无 daemon 进程时仍会自动拉起（不能把正常自愈路径改坏）。"""
    import mcp_raw

    monkeypatch.setattr(mcp_raw, "DAEMON_WAIT_SECONDS", 0)
    monkeypatch.setattr(mcp_raw.RealMCPClient, "_daemon_process_exists",
                        staticmethod(lambda: False))
    monkeypatch.setattr(mcp_raw.time, "sleep", lambda *_: None)
    spawned = []
    monkeypatch.setattr(mcp_raw.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or None)

    assert _make_bare_client().start() is False   # 拉起后仍连不上
    assert len(spawned) == 1, "确认无 daemon 时应自动拉起一次"


def test_daemon_process_exists_ignores_stale_pid(monkeypatch, tmp_path):
    """陈旧 PID 文件（进程已不存在 / 不是 daemon）不得被当成"有 daemon"。"""
    import mcp_daemon

    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(mcp_daemon, "PID_FILE", pid_file)

    assert mcp_daemon._daemon_process_exists() is False        # 文件不存在

    pid_file.write_text("not-a-pid")
    assert mcp_daemon._daemon_process_exists() is False        # 内容非法

    # 一个绝不可能存在的 PID
    pid_file.write_text("4194304")
    assert mcp_daemon._daemon_process_exists() is False

    # 活着的 pid，但身份不是守护进程（当前测试进程）→ 不作为"有 daemon"
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(mcp_daemon, "_pid_is_daemon", lambda pid: False)
    assert mcp_daemon._daemon_process_exists() is False

    # 身份无法判定（非 Linux）时保守视为"有 daemon"，宁可等待
    monkeypatch.setattr(mcp_daemon, "_pid_is_daemon", lambda pid: None)
    assert mcp_daemon._daemon_process_exists() is True
