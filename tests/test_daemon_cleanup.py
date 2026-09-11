"""双重 cleanup 回归。

`DaemonServer.start()` 里 `atexit.register(self.cleanup)`，而 `_shutdown()`
（SIGTERM/SIGINT 处理）也会调用 `cleanup()` —— 退出时 cleanup 会跑两遍，
历史上表现为日志里重复出现两行「守护进程已停止」。

要求：cleanup 必须幂等。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_daemon  # noqa: E402


class _StubMCP:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


def test_cleanup_is_idempotent(tmp_path, monkeypatch):
    # 关键：把 socket / pid 路径指向临时目录，绝不能碰真实 daemon 的文件
    monkeypatch.setattr(mcp_daemon, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(mcp_daemon, "PID_FILE", tmp_path / "daemon.pid")

    server = mcp_daemon.DaemonServer("fake-session")
    stub = _StubMCP()
    server.mcp = stub

    server.cleanup()
    server.cleanup()  # 模拟 atexit 再跑一次
    server.cleanup()

    assert stub.stop_calls == 1, "cleanup 必须幂等，不得重复停止 MCP 子进程"


def test_cleanup_logs_stopped_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(mcp_daemon, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(mcp_daemon, "PID_FILE", tmp_path / "daemon.pid")

    server = mcp_daemon.DaemonServer("fake-session")
    server.mcp = _StubMCP()

    with caplog.at_level("INFO", logger="mcp_daemon"):
        server.cleanup()
        server.cleanup()

    stopped = [r for r in caplog.records if "守护进程已停止" in r.getMessage()]
    assert len(stopped) == 1, f"「守护进程已停止」应只记录一次，实际 {len(stopped)} 次"
