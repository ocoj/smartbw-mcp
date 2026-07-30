"""
单元测试：验证 smartbw-mcp F-05 修复（DaemonClient 超时异常类型）
"""

import socket
import sys
import os

try:
    import pytest
except ImportError:
    pytest = None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ============================================================================
# Test F-05 — DaemonClient 超时应抛出 MCPTimeoutError
# ============================================================================

class TestDaemonClientTimeoutType:
    """验证 F-05: send_request 超时抛出 models.TimeoutError 而非 builtin.TimeoutError"""

    def test_timeout_raises_mcp_timeout_error(self):
        """DaemonClient.send_request 超时抛出的是 MCPTimeoutError（可通过熔断器捕获）"""
        import builtins

        from mcp_daemon import DaemonClient
        from models import TimeoutError as MCPTimeoutError

        client = DaemonClient(timeout=1)
        client.sock = _FakeTimeoutSocket()  # type: ignore[assignment]

        try:
            client.send_request("tools/call", {"name": "ping"})
            assert False, "应抛出异常"
        except MCPTimeoutError:
            pass  # 正确：抛出的是 models.TimeoutError
        except builtins.TimeoutError:
            assert False, "F-05 未修复：抛出的是内置 TimeoutError，熔断器无法捕获"
        except Exception as e:
            assert False, f"意外异常类型: {type(e).__name__}: {e}"
        finally:
            client.sock = None

    def test_circuit_breaker_catches_mcp_timeout_error(self):
        """熔断器 _with_circuit 能捕获 DaemonClient 抛出的超时异常"""
        from mcp_raw import RealMCPClient
        from models import TimeoutError as MCPTimeoutError

        client = RealMCPClient.__new__(RealMCPClient)
        client._failure_count = 0
        client._last_failure_time = 0.0
        client._circuit_open_until = 0.0
        client._max_failures = 5
        client._circuit_cooldown = 30.0

        client._daemon_client = _FakeDaemonClient(timeout_error=True)  # type: ignore[assignment]

        before_count = client._failure_count

        try:
            client._with_circuit(lambda: client._daemon_client.send_request("test", {}))
            assert False, "应抛出异常"
        except MCPTimeoutError:
            pass

        assert client._failure_count == before_count + 1, \
            f"F-05 未修复：熔断器未记录失败 (count={client._failure_count})"


# ============================================================================
# 测试辅助类
# ============================================================================

class _FakeTimeoutSocket:
    """模拟超时 socket：sendall 成功但 recv 总是抛出 socket.timeout"""
    def __init__(self):
        self._timeout = 1

    def sendall(self, data):
        pass

    def recv(self, _):
        raise socket.timeout("fake timeout")

    def settimeout(self, _):
        pass

    def close(self):
        pass


class _FakeDaemonClient:
    """模拟 DaemonClient，超时抛出 MCPTimeoutError"""
    def __init__(self, timeout_error=True):
        self._timeout_error = timeout_error

    def send_request(self, method, params):
        if self._timeout_error:
            from models import TimeoutError as MCPTimeoutError
            raise MCPTimeoutError("守护进程响应超时 (30s)")
        return {"result": "ok"}

    def close(self):
        pass


# ============================================================================
# 运行入口
# ============================================================================

if __name__ == "__main__":
    print("=" * 55)
    print("smartbw-mcp F-05 单元测试")
    print("=" * 55)

    passed = 0
    failed = 0

    tests = []
    for cls_name, cls in [(k, v) for k, v in locals().items()
                          if isinstance(v, type) and k.startswith('Test')]:
        for name in dir(cls):
            if name.startswith('test_'):
                tests.append((cls_name, name, getattr(cls(), name)))

    for cls_name, name, fn in tests:
        try:
            fn()
            print(f"  OK  {cls_name}.{name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {cls_name}.{name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {cls_name}.{name}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n结果: {passed}/{passed+failed} 通过")
    if failed == 0:
        print("OK 全部通过")
    else:
        print(f"FAIL {failed} 项失败")
    sys.exit(1 if failed > 0 else 0)
