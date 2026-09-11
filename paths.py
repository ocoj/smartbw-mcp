"""统一运行时路径解析。

`config.py` 与 `crypto_config.py` 必须对"配置目录在哪"有一致的答案：
- 默认 `~/.config/bitwarden-mcp/`
- 可由环境变量 `SMARTBW_CONFIG_DIR` 覆盖（支持 `~` 展开）

历史上 `crypto_config.py` 自行硬编码了默认路径，导致设置
`SMARTBW_CONFIG_DIR` 时"读取的配置"与"加密写回的配置"是两个不同文件
（加密承诺失效 + 跨目录凭据污染）。所有路径必须经由本模块，且以
**函数** 形式暴露 —— 不要在 import 期求值，否则路径会被冻结在进程启动时刻。
"""
import os
from pathlib import Path

DEFAULT_RUNTIME_SUBDIR = Path(".config") / "bitwarden-mcp"


def runtime_dir() -> Path:
    """运行时配置目录。默认 `~/.config/bitwarden-mcp/`，可由 `SMARTBW_CONFIG_DIR` 覆盖。"""
    custom = os.environ.get("SMARTBW_CONFIG_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / DEFAULT_RUNTIME_SUBDIR


def config_path() -> Path:
    """`config.json` 的完整路径。"""
    return runtime_dir() / "config.json"


def env_path() -> Path:
    """与 `config.json` 同目录的 `.env` 兜底文件路径。"""
    return runtime_dir() / ".env"


def is_default_runtime_dir() -> bool:
    """当前解析出的运行时目录是否就是默认目录（即未通过 SMARTBW_CONFIG_DIR 自定义）。"""
    return runtime_dir() == Path.home() / DEFAULT_RUNTIME_SUBDIR


DEFAULT_STATE_SUBDIR = Path(".smartbw-mcp")


def state_dir() -> Path:
    """**运行状态目录** —— 放 socket / pid / log / `NEEDS_REINIT` 标记。

    与 `runtime_dir()`（配置目录）是两回事：本目录一律为 `~/.smartbw-mcp/`，
    **不受 `SMARTBW_CONFIG_DIR` 影响**。测试要隔离它只能重定向 `HOME`
    （或只覆盖 socket，见 `socket_path()`）。

    历史上各模块各自硬编码 `Path.home() / ".smartbw-mcp"`，同一路径存在 4 处副本，
    与 P0-2（配置目录重复）同类的"多份真相"问题，故一并收归此处。
    """
    return Path.home() / DEFAULT_STATE_SUBDIR


def socket_path() -> Path:
    """daemon 的 Unix socket 路径。

    默认 `<state_dir>/daemon.sock`；可由 `SMARTBW_SOCKET_PATH` 单独覆盖。

    用途：真机集成测试需要"隔离 `HOME`（不污染真实运行目录）**同时**连上真实
    daemon" —— 二者曾互斥（见下）。有了本覆盖项，测试可只把 socket 指回真实路径，
    而不必放弃 `HOME` 隔离。
    """
    custom = os.environ.get("SMARTBW_SOCKET_PATH", "").strip()
    if custom:
        return Path(custom).expanduser()
    return state_dir() / "daemon.sock"
