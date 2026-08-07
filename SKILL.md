---
name: smartbw-mcp
description: 通过 MCP 工具访问 Vaultwarden/Bitwarden 密码管理器，支持模糊搜索项目、获取密码和 API Key、查询自定义字段。当需要获取密码、API 密钥、token、或查询 Vaultwarden 中的凭据时使用。
---

# SmartBW MCP

通过 MCP (Model Context Protocol) 访问 Vaultwarden/Bitwarden 密码管理器。

## MCP Server 配置

MCP Server 名是 `smartbw`（不是 `smartbw-mcp`）。工具名前缀取决于你的 MCP 客户端框架：

| 框架 | 工具名示例 | 配置方式 |
|------|-----------|----------|
| 通用 | `smartbw__<工具名>` | 在工具列表中搜索 `smartbw` 确认 |
| VS Code | `mcp__smartbw__<工具名>` | 详见 `docs/integrations/vscode.md`（含安装踩坑记录） |
| Deep Code | `mcp__smartbw__<工具名>` | 详见 `docs/integrations/deepcode.md` |
| OpenClaw | `smartbw__<工具名>` | 详见 `docs/integrations/openclaw.md` |

> 不要用 Python import 或 CLI，只通过 MCP 工具调用。

## 可用工具

| 工具短名 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `smartbw_search` | `query`, `limit=5` | 文本列表 | 模糊搜索项目名 |
| `smartbw_get_api` | `name`, `index?` | API Key 或结构化错误 | 获取 API 字段值 |
| `smartbw_get_password` | `name`, `index?` | 密码字符串 | 获取密码 |
| `smartbw_get_field` | `name`, `field`, `index?` | 字段值或结构化错误 | 获取任意自定义字段 |
| `smartbw_get_item` | `name`, `index?` | JSON 对象 | 获取项目完整信息（含密码） |
| `smartbw_list_all` | 无 | 文本列表 | 列出所有项目名称/用户名 |
| `smartbw_daemon_status` | 无 | 状态文本 | 检查守护进程 |
| `smartbw_sync_cache` | 无 | 结果文本 | 强制刷新：bw sync + 重启 MCP server + 清除缓存 |

## 操作模式

### 模式 0：缓存刷新（遇到数据不更新时优先执行）
1. 调 `smartbw_sync_cache`
2. 返回 `✅` 即完成，后续查询将拿到最新数据

### 模式 1：直接获取凭据
1. 调 `smartbw_get_api` 或 `smartbw_get_password`
2. 返回有效值 → 直接使用
3. 返回 `{"found": false, "available_fields": [...]}` → 从 available_fields 中选一个合适的字段，用 `smartbw_get_field` 获取

### 模式 2：多结果选择
1. 如果返回了 `{"matches": [...], "pick_one": true}`
2. **列出 matches 给用户选择**，每个选项显示 `[index] name | score`
3. 用户选了序号 N 后 → 同工具加 `index=N` 参数再调一次
4. 不要让用户重新输入名称

### 模式 3：浏览全部
1. 调 `smartbw_list_all` 查看所有项目

## 返回结果解析

- **成功获取密码/API Key**：直接返回字符串，即为凭据值
- **`smartbw_get_item` 单结果**：JSON `{"name": "...", "username": "...", "password": "...", "fields": {...}, "uris": [...]}`
- **`smartbw_get_item` 多结果**：JSON `{"matches": [{"index": 0, "name": "...", "score": 0.95}], "pick_one": true}`
- **字段不存在**：JSON `{"found": false, "item": "项目名", "available_fields": ["token", "endpoint"], "hint": "..."}`

## 常见错误

| 错误 | 原因 | 正确做法 |
|---|---|---|
| `No connection found` | Server 名是 `smartbw`（无连字符） | 确认 MCP 配置中 key 是 `smartbw` |
| `❌ 未找到` | 搜索词不精确或项目不存在 | 先用 `smartbw_search` 模糊搜，或 `smartbw_list_all` 浏览 |
| 返回 `{"pick_one": true}` 后不知所措 | 多结果需要用户选择 | 展示 matches 列表，等用户选序号后加 `index=N` 重试 |
| 工具名不存在 | 前缀取决于框架 | 在工具列表中搜 `smartbw` 确认实际前缀 |
| 搜不到刚添加/改名的项目 | MCP server 内部缓存未刷新 | 调 `smartbw_sync_cache` 刷新后再搜 |
