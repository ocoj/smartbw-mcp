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

真机模式（`SMARTBW_LIVE_TEST=1`）**同样隔离**，只把 daemon socket 指回真实运行目录
（`SMARTBW_SOCKET_PATH`）—— 这样"不污染真实环境"与"能连真实 daemon"不再互斥。
此前二者互斥，导致真机模式下**全部**用例失去隔离，且守护真实环境的
`test_reinit_marker_under_isolated_home` 恰好自行 skip（保护同时失效）。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 必须在改写 HOME 之前取到真实家目录，否则下面拿不到原值
_REAL_HOME = Path.home()

_ISOLATED_HOME = tempfile.mkdtemp(prefix="smartbw-test-home-")
os.environ["HOME"] = _ISOLATED_HOME                  # 隔离 ~/.smartbw-mcp 与 Path.home()
os.environ["SMARTBW_CONFIG_DIR"] = os.path.join(_ISOLATED_HOME, ".config", "bitwarden-mcp")

if os.environ.get("SMARTBW_LIVE_TEST"):
    # 真机用例要连真实 daemon，但只放行 socket 这一个路径，其余仍隔离。
    os.environ.setdefault(
        "SMARTBW_SOCKET_PATH", str(_REAL_HOME / ".smartbw-mcp" / "daemon.sock"))

