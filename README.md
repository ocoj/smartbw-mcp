# SmartBW MCP

> Vaultwarden/Bitwarden MCP 代理 — 让 AI 安全获取密码和 API Key

[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

SmartBW MCP 是一个 MCP (Model Context Protocol) 工具服务器，让 AI Agent 通过标准 MCP 接口安全访问你的 Vaultwarden/Bitwarden 密码库。

---

## 架构

```
smartbw_mcp_server.py              ← MCP 工具层（8 个工具，AI 零门槛调用）
        │
smart_search.py                    ← 智能搜索层（6 策略模糊搜索 + 索引加速）
        │
mcp_raw.py                         ← 通信层（JSON-RPC + 熔断保护）
        │
mcp_daemon.py                      ← 守护进程层（Unix Socket 常驻 + 自愈）
        │
@bitwarden/mcp-server (Node.js)    ← 协议层（Bitwarden 官方 MCP Server）
        │
Vaultwarden 服务器                  ← 数据层
```

---

## 快速开始

### 1. 安装依赖

```bash
npm install -g @bitwarden/cli @bitwarden/mcp-server
pip install cryptography
```

### 2. 配置

```bash
cp config.example.json ~/.config/bitwarden-mcp/config.json
# 编辑 config.json，填入你的 Vaultwarden 信息
```

`config.json` 示例：
```json
{
  "bw_host": "https://your-vaultwarden.example.com",
  "email": "user@example.com",
  "master_password": "YourMasterPassword",
  "client_id": "",
  "client_secret": ""
}
```

> 也可使用环境变量 `BW_HOST`、`BW_EMAIL`、`BW_MASTER_PASSWORD` 替代配置文件。

### 3. 启动守护进程

```bash
# 一键安装（含 systemd 服务或后台进程）
bash install.sh

# 或手动启动
python3 mcp_daemon.py
```

### 4. 配置 MCP 客户端

在你的 MCP 客户端中添加:

```json
{
  "mcpServers": {
    "smartbw": {
      "command": "python3",
      "args": ["/path/to/smartbw_mcp_server.py"]
    }
  }
}
```

---

## MCP 工具

| 工具 | 功能 |
|------|------|
| `smartbw_get_api` | 一行获取 API Key（自动搜索 API 字段） |
| `smartbw_get_password` | 获取密码 |
| `smartbw_get_field` | 获取任意自定义字段 |
| `smartbw_get_item` | 获取项目完整信息 |
| `smartbw_search` | 模糊搜索（名称/用户名/自定义字段） |
| `smartbw_list_all` | 列出所有项目 |
| `smartbw_daemon_status` | 检查守护进程状态 |
| `smartbw_sync_cache` | 强制刷新缓存（bw sync + 清理缓存） |

---

## 核心特性

- **模糊搜索**: 6 种评分策略，名称/用户名/自定义字段全覆盖，支持错别字容错
- **索引加速**: 首次加载后构建名称索引，精确/前缀匹配 O(1)
- **熔断保护**: 连续 5 次失败 → 30s 冷却，防止雪崩
- **线程安全**: socket 收发原子锁 + 缓存双重检查锁
- **自愈**: 守护进程每 60s 健康检查，session 过期自动恢复
- **凭证加密**: 主密码自动 Fernet 加密存储，密钥绑定本机指纹

---

## 安全

- Unix Socket 权限 `0o600`（umask + chmod 双重保护）
- 密码/API Key 通过 Vaultwarden 管理，不落盘
- 配置文件中的敏感字段自动加密（`!enc:v1:...`）
- **加密密钥绑定本机**，配置文件不可跨机器复制

---

## 配置参考

| 配置项 | 环境变量 | 说明 |
|--------|----------|------|
| Vaultwarden 地址 | `BW_HOST` | 如 `https://vault.example.com` |
| 登录邮箱 | `BW_EMAIL` | |
| 主密码 | `BW_MASTER_PASSWORD` | 会自动加密存储 |
| API Key | `BW_CLIENTID` / `BW_CLIENTSECRET` | 推荐，兼容 2FA |
| 自定义配置目录 | `SMARTBW_CONFIG_DIR` | 默认 `~/.config/bitwarden-mcp/` |
| MCP 超时 | `SMARTBW_MCP_TIMEOUT` | 默认 30s |
| 模糊搜索阈值 | `SMARTBW_FUZZY_THRESHOLD` | 默认 0.5 |

---

## 文档

| 文档 | 说明 |
|------|------|
| [docs/architecture.md](docs/architecture.md) | 架构设计与通信路径 |
| [docs/dependencies.md](docs/dependencies.md) | 依赖与版本要求 |
| [docs/integrations/deepcode.md](docs/integrations/deepcode.md) | Deep Code 集成 |
| [docs/integrations/openclaw.md](docs/integrations/openclaw.md) | OpenClaw 集成 |
| [reference/troubleshooting.md](reference/troubleshooting.md) | 故障排查 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |

---

## 许可证

MIT
