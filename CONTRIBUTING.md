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
- 保持代码风格与现有一致（Python 3.8+，4 空格缩进）

## 安全提醒

- **绝不要**在代码、文档、commit message 中写入真实密码、域名或 IP
- 配置文件模板只能放占位符
- 如发现安全漏洞，请私下联系而不要开公开 Issue
