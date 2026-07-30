"""Smoke tests: verify all core imports work."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_config_import():
    from config import CONFIG, DEFAULT_TIMEOUT, FUZZY_THRESHOLD, get_config
    assert DEFAULT_TIMEOUT == 30
    assert 0 < FUZZY_THRESHOLD <= 1
    assert isinstance(CONFIG, dict)


def test_models_import():
    from models import BwItem, ConnectionError, LockedError, SearchResult, TimeoutError
    item = BwItem(id="test", name="Test")
    assert item.name == "Test"
    assert issubclass(TimeoutError, Exception)
    assert issubclass(LockedError, Exception)


def test_mcp_raw_import():
    from mcp_raw import RealMCPClient
    assert RealMCPClient is not None


def test_smart_search_import():
    from smart_search import SmartBitwardenMCP, get_password_smart, get_smart_mcp, _normalize, _fuzzy_score
    assert SmartBitwardenMCP is not None
    assert callable(get_smart_mcp)
    assert callable(get_password_smart)
    # 归一化
    assert _normalize("  Hello  ") == "hello"
    assert _normalize(None) == ""
    assert _normalize("GitHub") == "github"
    # 模糊评分
    assert _fuzzy_score("github", "github") > 0.9
    assert _fuzzy_score("github", "GitHub Login") > 0.5
    assert _fuzzy_score("github", "Netflix Movie") < 0.5


def test_mcp_daemon_import():
    from mcp_daemon import DaemonClient, DaemonServer, MCPServerManager
    assert DaemonClient is not None
    assert MCPServerManager is not None


def test_unlock_import():
    from unlock import auto_unlock
    assert callable(auto_unlock)


def test_crypto_config_import():
    from crypto_config import encrypt_value, decrypt_value, process_config_on_startup
    assert callable(encrypt_value)
    assert callable(decrypt_value)


if __name__ == "__main__":
    import traceback
    passed = 0
    failed = 0
    for name, fn in list(locals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n结果: {passed}/{passed+failed} 通过")
    sys.exit(1 if failed else 0)
