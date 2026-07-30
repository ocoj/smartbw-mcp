# SmartBW MCP — OpenClaw 集成指南

## 前置条件

OpenClaw 的 skill 依赖声明写在 `SKILL.md` frontmatter 中：

```yaml
openclaw:
  requires:
    bins: ["bw", "node", "python3"]
    env: ["BW_HOST", "BW_EMAIL", "BW_MASTER_PASSWORD"]
```

## 部署

OpenClaw 的 skill 目录为 `~/.openclaw/workspace/skills/<skill-name>/`

```bash
PROJECT=/path/to/smartbw-mcp
SKILL_DIR=~/.openclaw/workspace/skills/smartbw-mcp
mkdir -p $SKILL_DIR
cp $PROJECT/{*.py,SKILL.md} $SKILL_DIR/
```

## MCP Server 配置

编辑 `openclaw.json`（项目级）或 `~/.openclaw/openclaw.json`（全局），在 `mcpServers` 中添加：

```json
{
  "mcpServers": {
    "smartbw": {
      "type": "stdio",
      "command": "python3",
      "args": ["$HOME/.openclaw/workspace/skills/smartbw-mcp/smartbw_mcp_server.py"]
    }
  }
}
```

> ⚠️ Server 名必须是 `smartbw`（不带连字符）
> ⚠️ OpenClaw 中工具名格式为 `smartbw__<工具名>`（如 `smartbw__smartbw_get_api`）

重启 OpenClaw 或新开会话后生效。

## 验证

```bash
# daemon 运行中
systemctl --user status smartbw-daemon

# 在 OpenClaw 会话中测试
"请帮我查一下 Vaultwarden 里有哪些密码项目"
```
