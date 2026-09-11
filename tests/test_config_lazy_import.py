"""D8 回归：import 期不得触发配置解析 / MCP 路径自动发现（npm、which 子进程）。

历史问题：`mcp_raw.py`、`unlock.py` 在模块顶层写 `from config import CONFIG`，
这会立刻触发 config 的模块级 `__getattr__` → `get_config()` → `_find_mcp_path()`，
在全新安装（config.json 里 mcp_server_path 为空）时起 npm/which 子进程，
使 config 里"避免 import 时即执行 npm/which"的惰性加载形同虚设。

这里用子进程跑，因为"import 期行为"每进程只能观察一次。
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)

_PROBE = textwrap.dedent(
    """
    import os, subprocess, sys
    sys.path.insert(0, os.getcwd())
    spawned = []
    _orig = subprocess.run
    def spy(*a, **k):
        spawned.append(a[0] if a else k.get("args"))
        return _orig(*a, **k)
    subprocess.run = spy
    import mcp_raw, unlock      # 这两个模块的顶层 import 曾触发自动发现
    print(spawned)
    """
)


def test_import_does_not_spawn_discovery(tmp_path):
    """空配置目录（等同全新安装）下 import，不得起任何子进程。"""
    env = dict(os.environ)
    env["SMARTBW_CONFIG_DIR"] = str(tmp_path)  # 空目录 → 走自动发现分支

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"探测脚本失败：{proc.stderr}"
    assert proc.stdout.strip() == "[]", (
        f"import 期不得起子进程，实际触发：{proc.stdout.strip()}"
    )


def test_get_config_is_cached():
    """get_config() 必须进程内缓存，避免反复读文件 / 重复跑自动发现。"""
    import config

    first = config.get_config()
    assert config.get_config() is first, "重复调用应返回同一缓存对象"
    assert config.get_config(refresh=True) is not first, "refresh=True 应强制重读"
