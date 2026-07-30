# 架构设计

## 分层架构

```
┌─────────────────────────────────────────────────┐
│ smartbw_mcp_server.py    MCP 工具层              │
│ stdio JSON-RPC, 8 个工具, 优雅升级              │
├─────────────────────────────────────────────────┤
│ smart_search.py          智能搜索层              │
│ 6 策略模糊搜索, 索引加速, 缓存 (TTL 30s)        │
├─────────────────────────────────────────────────┤
│ mcp_raw.py               通信层                  │
│ JSON-RPC, 熔断器 (5次/30s), CRUD 操作           │
├─────────────────────────────────────────────────┤
│ mcp_daemon.py            守护进程层               │
│ Unix Socket 常驻, 子进程管理, 自愈, 并发         │
├─────────────────────────────────────────────────┤
│ @bitwarden/mcp-server    Node.js 协议层           │
│ Bitwarden 官方 MCP Server (npm)                  │
├─────────────────────────────────────────────────┤
│ Vaultwarden 服务器        数据层                  │
└─────────────────────────────────────────────────┘

辅助模块:
  config.py         配置加载 (环境变量 / .env / config.json)
  crypto_config.py  凭证加密 (HKDF-SHA256 + Fernet)
  unlock.py         自动登录/解锁
  models.py         数据类型与异常
```

## 通信路径

```
AI Agent
  ↓ stdio JSON-RPC
smartbw_mcp_server.py     → 工具注册 + 请求分发
  ↓ Unix Socket
mcp_daemon.py             → 常驻进程池 + 健康检查
  ↓ stdio
node @bitwarden/mcp-server → MCP 协议适配
  ↓ HTTPS
Vaultwarden 服务器          → 密码存储
```

全链路无进程 fork 开销，单一通信路径。

## 核心机制

### 熔断器

- 阈值：连续 5 次失败
- 冷却：30 秒
- 重置窗口：120 秒内无失败
- 触发条件：LockedError / TimeoutError / ConnectionError / OSError

### 优雅升级

信号文件 `~/.smartbw-mcp/restart.signal`：
1. 部署脚本 `touch` 此文件
2. MCP Server 处理完当前请求后检测到 → `exit(0)`
3. AI 客户端发现 stdio 关闭 → 自动 spawn 新进程加载新代码

### 凭证加密

```
密钥派生: HKDF-SHA256(hostname + machine-id, salt, info)
加密算法: Fernet (AES-128-CBC + HMAC-SHA256)
存储格式: "!enc:v1:{base64url(ciphertext)}"
```

密钥绑定本机指纹，配置文件不可跨机器复制。

### 搜索算法

6 维度 BM25 风格评分：
- 精确匹配: 1.0
- 子串匹配 (≥3字符): 0.85
- 短查询 (<3字符): 0.50
- 反向子串: 0.80
- SequenceMatcher 兜底
- 自定义字段: 名 ×1.2, 值 ×0.8

配合名称索引：精确/前缀匹配 O(1)。

## 模块列表

| 模块 | 职责 | 核心类/函数 |
|------|------|-------------|
| `smartbw_mcp_server.py` | MCP 工具注册 + 请求分发 | `_get_client_ctx()`, `_resolve_search()` |
| `smart_search.py` | 智能模糊搜索 + 缓存 | `SmartBitwardenMCP`, `_fuzzy_score()` |
| `mcp_raw.py` | JSON-RPC 通信 + 熔断 | `RealMCPClient`, `_with_circuit()` |
| `mcp_daemon.py` | Unix Socket + 子进程管理 | `DaemonServer`, `MCPServerManager` |
| `unlock.py` | bw CLI 自动登录/解锁 | `auto_unlock()` |
| `crypto_config.py` | 凭证加密/解密 | `process_config_on_startup()` |
| `config.py` | 配置加载 + 路径发现 | `get_config()`, `_find_mcp_path()` |
| `models.py` | 数据类型与异常 | `BwItem`, `SearchResult` |
