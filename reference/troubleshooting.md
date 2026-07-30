# SmartBW MCP 故障排查

## 错误分类

MCP 工具返回的错误带有 `[分类X·]` 前缀，根据分类判断原因和恢复方式。

### 分类A — 守护进程未运行

```
[分类A·守护进程未运行] smartbw-daemon 守护进程未启动或正在重启中。
```

| 属性 | 值 |
|------|-----|
| **恢复方式** | ✅ 自动恢复 — daemon 会在 10s 内自动重启 |
| **典型原因** | daemon 被重启、系统刚启动、进程意外退出 |
| **手动恢复** | `systemctl --user restart smartbw-daemon`（或直接重启 python3 mcp_daemon.py） |
| **排查** | `tail -30 ~/.smartbw-mcp/daemon.log` |

### 分类B — Vaultwarden 服务超时/不可达

```
[分类B·服务超时] Vaultwarden 服务器响应超时，守护进程正在自动重试。
```

| 属性 | 值 |
|------|-----|
| **恢复方式** | ✅ 自动恢复 — daemon 每 10s 重试一次，最多 5 次 |
| **典型原因** | Vaultwarden 服务宕机、网络中断 |
| **排查** | `tail -30 ~/.smartbw-mcp/daemon.log` 查看解锁进度 |

### 分类C — 凭证/主密码问题

```
[分类C·凭证问题] 守护进程无法解锁 Vaultwarden 金库。
```

| 属性 | 值 |
|------|-----|
| **恢复方式** | ❌ 需人工干预 |
| **典型原因** | 1) 主密码被修改/清空 2) NEEDS_REINIT 标记存在 3) config.json 损坏 |
| **排查步骤** | ① 检查 `~/.config/bitwarden-mcp/config.json` 中 `master_password` 是否为 `!enc:v1:` 前缀<br>② 检查 `~/.config/bitwarden-mcp/.env` 中 `BW_MASTER_PASSWORD` 是否存在<br>③ 检查 `~/.smartbw-mcp/NEEDS_REINIT` 标记文件是否存在<br>④ `tail -30 ~/.smartbw-mcp/daemon.log` 查看详细错误 |
| **修复** | 如有 NEEDS_REINIT → 编辑 config.json 填入明文 master_password → 重启 daemon（会自动重新加密） |

### 分类D — 熔断保护

```
[分类D·熔断保护] smartbw-mcp 连续失败已达上限，Ns 后自动重试。
```

| 属性 | 值 |
|------|-----|
| **恢复方式** | ✅ 自动恢复 — 冷却 30s 后自动重试 |
| **典型原因** | 连续 5 次请求失败，熔断器打开 |

## 快速诊断命令

```bash
# 查看 daemon 状态
systemctl --user status smartbw-daemon

# 查看最近日志
tail -30 ~/.smartbw-mcp/daemon.log

# 手动重启 daemon
systemctl --user restart smartbw-daemon

# 检查加密凭据
python3 -c "
import json
from pathlib import Path
cfg_path = Path.home() / '.config' / 'bitwarden-mcp' / 'config.json'
with open(cfg_path) as f:
    cfg = json.load(f)
pw = cfg.get('master_password','')
print('已加密' if pw.startswith('!enc:') else '明文' if pw else '空')
"
```
