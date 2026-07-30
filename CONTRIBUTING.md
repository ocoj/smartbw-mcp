# Contributing to SmartBW MCP

感谢你的贡献！以下指引帮助你快速上手。

## 开发环境搭建

```bash
git clone https://github.com/<your-org>/smartbw-mcp.git
cd smartbw-mcp

# 安装依赖
pip install pytest cryptography
npm install -g @bitwarden/cli @bitwarden/mcp-server

# 安装 pre-commit hooks
pip install pre-commit && pre-commit install
```

## 配置

```bash
cp config.example.json ~/.config/bitwarden-mcp/config.json
# 编辑 config.json 填入你的 Vaultwarden 信息
```

## 运行测试

```bash
pytest tests/ -v
# 或直接运行
python3 tests/test_imports.py
```

## 提交规范

- 使用语义化 commit message：`fix:` / `feat:` / `docs:` / `refactor:`
- 提交前会自动运行 `scripts/pre-commit.sh` 检查敏感信息泄露

## 版本号规则

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

| 版本位 | 触发条件 | 示例 |
|--------|----------|------|
| **主版本 (X)** | 不兼容的 API 变更、移除 MCP 工具、修改配置格式 | 2.0.0 → 3.0.0 |
| **次版本 (Y)** | 新增向下兼容的功能、新增 MCP 工具 | 2.2.0 → 2.3.0 |
| **修订号 (Z)** | Bug 修复、文档更新、内部重构 | 2.2.7 → 2.2.8 |

版本号同时在以下位置更新：
- `pyproject.toml` → `version = "X.Y.Z"`
- `smartbw_mcp_server.py` → `"version": "X.Y.Z"`
- `CHANGELOG.md` → `## [X.Y.Z]`
- 保持代码风格与现有一致（Python 3.8+，4 空格缩进）

## 安全提醒

- **绝不要**在代码、文档、commit message 中写入真实密码、域名或 IP
- 配置文件模板只能放占位符
- 如发现安全漏洞，请私下联系而不要开公开 Issue
