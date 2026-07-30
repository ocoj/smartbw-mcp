# 依赖与版本要求

## 系统依赖

| 工具 | 最低版本 | 安装方式 | 用途 |
|------|----------|----------|------|
| Python | 3.8+ | 系统包管理器 | 核心运行时 |
| Node.js | 16+ | 系统包管理器 | bw CLI + MCP Server |
| npm | 7+ | 随 Node.js | 安装 JS 包 |
| [Bitwarden CLI](https://bitwarden.com/help/cli/) (`bw`) | 2024.2+ | `npm install -g @bitwarden/cli` | Vaultwarden 通信 |
| [Bitwarden MCP Server](https://github.com/bitwarden/mcp-server) | 2024.11+ | `npm install -g @bitwarden/mcp-server` | MCP 协议层 |

## Python 依赖

| 包 | 版本 | 必需 | 用途 |
|----|------|------|------|
| `cryptography` | 3.0+ | ✅ | 凭证加密 (Fernet/HKDF) |
| `pytest` | 7.0+ | 开发 | 测试框架 |
| `pytest-cov` | 4.0+ | 开发 | 覆盖率 |
| `ruff` | 0.1+ | 开发 | Lint/格式化 |
| `black` | 23.0+ | 开发 | 代码格式化 |
| `isort` | 5.0+ | 开发 | Import 排序 |

安装：
```bash
pip install cryptography           # 生产
pip install "smartbw-mcp[dev]"     # 开发（含全部开发依赖）
```

## MCP 协议支持

| 协议字段 | 值 | 说明 |
|----------|-----|------|
| 协议版本 | `2024-11-05` | JSON-RPC 2.0 over stdio |
| 传输方式 | stdio | MCP Server 与客户端之间 |
| 内部传输 | Unix Socket | Daemon 与 MCP Server 之间 |
| 工具数量 | 8 | smartbw_search/get_api/get_password/get_field/get_item/list_all/daemon_status/sync_cache |

## 操作系统支持

| 系统 | daemon 管理 | 状态 |
|------|-------------|------|
| Linux (systemd) | `systemctl --user` | ✅ 完整支持 |
| Linux (非 systemd) | 后台进程 (`nohup`) | ✅ 支持 |
| macOS | 后台进程 (`nohup`) | ✅ 支持 |
| Windows WSL | 后台进程 | ⚠️ 需测试 |

## Vaultwarden/Bitwarden 兼容性

| 服务端 | 版本 | 状态 |
|--------|------|------|
| Vaultwarden | 1.30+ | ✅ 完整支持 |
| Bitwarden (官方) | 2024+ | ✅ 应兼容（bw CLI 统一接口） |
| Bitwarden (自建) | 2024+ | ✅ 应兼容 |
