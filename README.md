# SmartBW MCP

> Vaultwarden/Bitwarden MCP 代理 — 让 AI 安全获取密码和 API Key

[![CI](https://github.com/ocoj/smartbw-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ocoj/smartbw-mcp/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/release/ocoj/smartbw-mcp?label=version&color=blue)](https://github.com/ocoj/smartbw-mcp/releases)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-122%20passed-brightgreen)](https://github.com/ocoj/smartbw-mcp/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-47%25-yellow)](https://github.com/ocoj/smartbw-mcp/actions/workflows/ci.yml)

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
mcp_daemon.py                      ← 守护进程层（Unix Socket 常驻 + 自愈 + 单实例保护）
        │
@bitwarden/mcp-server (Node.js)    ← 协议层（Bitwarden 官方 MCP Server）
        │
Vaultwarden 服务器                  ← 数据层
```

```
paths.py                           ← 横切：统一运行时路径（配置目录 / 运行状态目录）
config.py · crypto_config.py · unlock.py · models.py
                                   ← 支撑：配置加载 / 凭证加密 / 自动解锁 / 数据类型
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
| `smartbw_sync_cache` | 强制刷新（bw sync + 重启 MCP server + 清缓存） |

---

## 核心特性

- **模糊搜索**: 6 种评分策略，名称/用户名/自定义字段全覆盖，支持错别字容错
- **索引加速**: 首次加载后构建名称索引，精确/前缀匹配 O(1)
- **短 TTL 缓存**: 项目列表常驻内存，TTL 15s 按需刷新（**无定时器**）；过期即同步刷新，无结果/结果可疑自动重查；无人查询时零后端请求
- **熔断保护**: 连续 5 次失败 → 30s 冷却，防止雪崩
- **线程安全**: socket 收发原子锁 + 缓存 single-flight 锁（并发查询只实际拉取一次）
- **自愈**: 守护进程每 60s 健康检查，session 过期自动恢复
- **凭证加密**: `master_password` / `client_secret` / `api_key` 自动 Fernet 加密存储，密钥绑定本机指纹

---

## 安全

- Unix Socket 权限 `0o600`（umask + chmod 双重保护）
- 日志目录 `0o700` / 日志文件 `0o600`（**含每日轮转后新建的当前日志**），日志中的账号以掩码记录
- 密码/API Key 由 Vaultwarden 托管，本地仅按需读取
- 配置文件中的敏感字段自动加密（`!enc:v1:...`），含 `master_password` / `client_secret` / `api_key`
- **加密密钥绑定本机**，配置文件不可跨机器复制

---

## 配置参考

| 配置项 | 环境变量 | 说明 |
|--------|----------|------|
| Vaultwarden 地址 | `BW_HOST` | 如 `https://vault.example.com` |
| 登录邮箱 | `BW_EMAIL` | |
| 主密码 | `BW_MASTER_PASSWORD` | 会自动加密存储 |
| API Key | `BW_CLIENTID` / `BW_CLIENTSECRET` | 推荐，兼容 2FA；`client_secret` 会自动加密 |
| API Key（单字段） | `BW_API_KEY` | `user.clientId.clientSecret` 格式，自动拆分；存入 config.json 的 `api_key` 时同样加密 |
| MCP Server 路径 | `MCP_SERVER_PATH` / `BITWARDEN_MCP_SERVER_PATH` | 留空自动发现（npm/which/常见路径） |
| 自定义配置目录 | `SMARTBW_CONFIG_DIR` | 默认 `~/.config/bitwarden-mcp/`；支持 `~` 展开，读写与加密均以该目录为准 |
| daemon Socket 路径 | `SMARTBW_SOCKET_PATH` | 默认 `~/.smartbw-mcp/daemon.sock`。仅覆盖 socket 一个路径，便于"隔离 HOME 但连真实 daemon"（如真机测试） |
| daemon 启动宽限 | `SMARTBW_DAEMON_WAIT` | 默认 5s；连不上时先反复重试该时长再考虑拉起新实例（设 `0` 关闭），避免重启窗口内造出第二个 daemon |
| MCP 超时 | `SMARTBW_MCP_TIMEOUT` | 默认 30s |
| 模糊搜索阈值 | `SMARTBW_FUZZY_THRESHOLD` | 默认 0.5 |
| 缓存 TTL | `SMARTBW_CACHE_TTL` | 默认 15s；过期即同步刷新，无定时器 |
| 强制刷新最小间隔 | `SMARTBW_CACHE_REFRESH_MIN_AGE` | 默认 5s；避免一次查询重复拉取 |
| 结果可疑阈值 | `SMARTBW_CACHE_SUSPICIOUS_SCORE` | 默认 0.6；须 > 模糊搜索阈值，否则该分支不生效 |
| 自动解锁 | `SMARTBW_AUTO_UNLOCK` | 默认 1（设为 `0` 关闭） |
| 自动解锁重试次数 | `SMARTBW_MAX_UNLOCK_ATTEMPTS` | 默认 3 |
| CLI 超时（status/login/unlock/discovery） | `SMARTBW_CLI_STATUS_TIMEOUT` / `SMARTBW_CLI_LOGIN_TIMEOUT` / `SMARTBW_CLI_UNLOCK_TIMEOUT` / `SMARTBW_CLI_DISCOVERY_TIMEOUT` | 默认 10 / 15 / 15 / 10s |
| 日志级别 / 日志文件 | `LOG_LEVEL` / `LOG_FILE` | `LOG_LEVEL` 影响 MCP server（默认 `INFO`）与 `python3 config.py`；守护进程固定 `INFO`。`LOG_FILE` 仅 `python3 config.py` 生效 |

---

## 开发与测试

```bash
pip install -e ".[dev]"     # pytest / pytest-cov / ruff / black

python -m ruff check .                                   # lint（CI 阻断项）
python -m pytest -q --cov=. --cov-report=term-missing --cov-fail-under=40
```

| 项目 | 当前状态 |
|------|----------|
| 测试 | 122 项通过、2 项跳过 |
| 覆盖率 | 47%（CI 门槛 40%，低于则构建失败） |
| Lint | `ruff check` 全绿（CI 阻断项） |
| CI 矩阵 | Python 3.8 / 3.10 / 3.12 + wheel 打包内容校验 |

测试默认**完全隔离**：`tests/conftest.py` 会把 `HOME` 与 `SMARTBW_CONFIG_DIR` 指向临时目录，
不读写你的真实配置。只有显式设置 `SMARTBW_LIVE_TEST=1` 的用例才连接真实 daemon，
其余需要真机的用例自动跳过。

顶部 Tests / Coverage 徽章为静态值，与上表同源，发版时同步更新。

发版流程（三处版本号一致性校验、CI 自动打 tag 与创建 Release）与推送前脱敏审计见
[CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 文档

| 文档 | 说明 |
|------|------|
| [docs/architecture.md](docs/architecture.md) | 架构设计与通信路径 |
| [docs/dependencies.md](docs/dependencies.md) | 依赖与版本要求 |
| [docs/integrations/deepcode.md](docs/integrations/deepcode.md) | Deep Code 集成 |
| [docs/integrations/openclaw.md](docs/integrations/openclaw.md) | OpenClaw 集成 |
| [docs/integrations/vscode.md](docs/integrations/vscode.md) | VS Code 集成（含安装踩坑记录） |
| [reference/troubleshooting.md](reference/troubleshooting.md) | 故障排查 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |

---

## 许可证

MIT
