# SmartBW MCP — VS Code 集成指南（含安装踩坑记录）

> 本文档基于实际安装经验整理，记录了 VS Code 接入 smartbw 的正确方法，
> 以及最容易踩的坑（配置不生效、工具超限等）。

## 前置条件

- VS Code **1.129+**（MCP 配置位置与此版本强相关，见下文）
- 已安装依赖并启动守护进程：`bash install.sh`（或手动 `python3 mcp_daemon.py`）
- daemon 运行中：`systemctl --user status smartbw-daemon`

## 配置位置（VS Code 1.129+ 的关键变更）

VS Code 1.129 起，MCP server 配置**已从 `settings.json` 迁移到 profile 专用的 `mcp.json` 文件**。
写在 `settings.json` 的 `mcp.servers`（旧格式）**不会生效**——VS Code 不会加载它，日志里也不会有任何启动记录。

`mcp.json` 有三个可用位置，按作用域选择：

| 作用域 | 路径 | 说明 |
|--------|------|------|
| 用户级（远程/SSH） | `~/.vscode-server/data/User/mcp.json` | 远程开发（SSH / WSL / Dev Container）场景 |
| 用户级（本地） | `~/.config/Code/User/mcp.json` | 本地 VS Code |
| 项目级 | `.vscode/mcp.json`（项目根目录） | 仅当前工作区生效，随仓库分发 |

> 项目级 `.vscode/mcp.json` 也会被 Cline 等支持 MCP 的插件读取。

## 配置文件

在 `mcp.json` 中添加：

```json
{
  "servers": {
    "smartbw": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/smartbw_mcp_server.py"]
    }
  }
}
```

> ⚠️ **顶层结构是 `{"servers": ...}`**，不是 `{"mcp": {"servers": ...}}`（这是最常写错的点）
> ⚠️ **Server 名必须是 `smartbw`**（不带连字符）
> ⚠️ `command`/`args` 请使用绝对路径，否则远程场景可能解析失败

## 生效与验证

1. 编辑保存 `mcp.json` 后，`Ctrl+Shift+P` → `Developer: Reload Window` 重新加载窗口
2. 验证 MCP server 握手：应看到 `smartbw` 连接成功（v2.3.0，8 个工具齐全）
3. 在 Chat 中测试：

   ```text
   请帮我查一下 Vaultwarden 里有哪些密码项目
   ```

4. VS Code 中工具名格式为 `mcp__smartbw__<工具名>`（如 `mcp__smartbw__smartbw_get_api`），实际前缀以工具列表中搜 `smartbw` 的结果为准

## 踩坑记录（真实安装经验）

以下是安装 smartbw 时实际踩过的坑，按发生顺序列出：

### 坑 1：配置写在 settings.json，完全不生效

| 项 | 值 |
|----|-----|
| **症状** | VS Code 日志中没有任何 smartbw 启动记录，工具列表里找不到 smartbw |
| **原因** | VS Code 1.129 已把 MCP server 配置从 `settings.json` 迁移到 profile 专用的 `mcp.json`，旧格式被忽略 |
| **解决** | 删除 `settings.json` 中的旧 `mcp` 配置，改用 `~/.vscode-server/data/User/mcp.json`（远程）或 `.vscode/mcp.json`（项目级） |

### 坑 2：顶层结构写错

| 项 | 值 |
|----|-----|
| **症状** | 配置了 mcp.json 但仍不加载 |
| **原因** | 写成 `{"mcp": {"servers": ...}}`，多包了一层 `mcp` |
| **解决** | 顶层直接是 `{"servers": ...}` |

### 坑 3：工具超限被丢弃

| 项 | 值 |
|----|-----|
| **症状** | 日志报 `Had to drop N tools due to limit constraints`，smartbw 的部分/全部工具不可用 |
| **原因** | Copilot Chat 单请求工具上限（默认 128），安装的 MCP 工具过多时按 embedding 聚类丢弃超限 singleton |
| **解决** | 在 VS Code `settings.json`（用户级）中调高虚拟工具阈值：`{ "github.copilot.chat.virtualTools.threshold": 128 }` |

### 坑 4：工具仍不出现

| 项 | 值 |
|----|-----|
| **症状** | Reload Window 后工具还是没出现 |
| **原因** | 工具被丢弃/未启用 |
| **解决** | 在 Chat 输入框下方点 **Configure Tools** 按钮，勾选启用 `smartbw` 的 8 个工具 |

## 故障排查速查

```bash
# daemon 状态
systemctl --user status smartbw-daemon

# 最近日志（看启动/解锁/熔断）
tail -30 ~/.smartbw-mcp/daemon.log

# 手动重启 daemon
systemctl --user restart smartbw-daemon

# 确认 mcp.json 位置与内容
ls -la ~/.vscode-server/data/User/mcp.json   # 远程场景
ls -la .vscode/mcp.json                      # 项目级
```

详见 [reference/troubleshooting.md](../../reference/troubleshooting.md) 的错误分类（A~D）。
