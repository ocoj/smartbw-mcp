# SmartBW MCP — Deep Code 集成指南

## MCP Server 配置

编辑 `~/.deepcode/settings.json`，在 `mcpServers` 中添加：

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

> ⚠️ Server 名必须是 `smartbw`
> ⚠️ Deep Code 中工具名格式为 `mcp__smartbw__<工具名>`（如 `mcp__smartbw__smartbw_get_api`）

使用 `/mcp` 命令验证服务器是否运行。

## 验证

在 Deep Code 会话中测试：
```
请帮我查一下 Vaultwarden 里有哪些密码项目
```
