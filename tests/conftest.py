"""Pytest configuration for smartbw-mcp.

全局隔离：测试**绝不允许**写用户真实运行环境。需要隔离的路径有两类：

1. 配置目录 `~/.config/bitwarden-mcp/` —— `config.get_config()` 会经
   `crypto_config.process_config_on_startup()` 把明文敏感字段加密**写回** config.json。
2. 运行目录 `~/.smartbw-mcp/` —— `crypto_config.REINIT_FILE`（`NEEDS_REINIT`）、
   `mcp_daemon` 的 `daemon.sock` / `daemon.pid` / `daemon.log` 都硬编码在 `Path.home()` 下，
   **不受 `SMARTBW_CONFIG_DIR` 约束**。曾因只隔离了后者，导致"密文解密失败"用例把
   `NEEDS_REINIT` 写进了用户真实的 `~/.smartbw-mcp/`。

因此这里同时重定向 `HOME` 与 `SMARTBW_CONFIG_DIR` 到临时目录。
个别测试用 monkeypatch 覆盖是在测试内生效，属于测试自己的事。

例外：显式设置 `SMARTBW_LIVE_TEST=1` 时**不做隔离** —— 真机集成测试
（`tests/test_cache_live.py`）需要连真实 daemon 与 Vaultwarden，隔离后只会被 skip。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if not os.environ.get("SMARTBW_LIVE_TEST"):
    _ISOLATED_HOME = tempfile.mkdtemp(prefix="smartbw-test-home-")
    os.environ["HOME"] = _ISOLATED_HOME                  # 隔离 ~/.smartbw-mcp 与 Path.home()
    os.environ["SMARTBW_CONFIG_DIR"] = os.path.join(_ISOLATED_HOME, ".config", "bitwarden-mcp")
