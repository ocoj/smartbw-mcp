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

真机集成测试（会访问真实 Vaultwarden，**默认跳过**，仅执行读操作），需 daemon 运行中：

```bash
SMARTBW_LIVE_TEST=1 pytest tests/test_cache_live.py -v
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

### 一键同步（不要手改三处）

```bash
bash scripts/bump-version.sh --auto     # 依据 conventional commits 自动推导版本
bash scripts/bump-version.sh 2.4.0      # 或显式指定
```

`--auto` 自最新 tag 统计提交并推导级别（`BREAKING`/`!` → major、`feat` → minor、
其余 → patch），同步上述三处，并生成**按类型分组的 CHANGELOG 骨架**（逐条列出提交
主题，细节仍需人工补全）。若检测到"已 bump 但尚未发版"，它会拒绝再次递增。

### 发版流程

1. `bash scripts/bump-version.sh --auto` —— 推导版本 + 生成 CHANGELOG 骨架
2. 补全 CHANGELOG 正文（说明**为什么**改、如何验证）
3. `git add -A && git commit -m "release: X.Y.Z ..."` 并推送到 `main`
4. **CI 自动收尾**：`.github/workflows/release.yml` 读取 `pyproject.toml` 版本，
   校验三处一致后，若对应 tag 不存在则**自动打注解 tag + 从 CHANGELOG 提取发行说明
   创建 GitHub Release**（已存在则跳过，幂等 —— 因此每次 push 都跑也无副作用）

### 推送前审计

```bash
bash scripts/pre-push-audit.sh              # 对比 origin/main
bash scripts/pre-push-audit.sh --range v2.3.7   # 指定基线
```

比 `pre-commit` 覆盖更广：全量跟踪文件内容、**待推送提交的新增行**、提交作者邮箱、
不应被跟踪的文件、tag 注解。命中时**只打印「文件:行号」不打印内容**，避免审计本身
造成二次泄露。

## 安全提醒

- **绝不要**在代码、文档、commit message 中写入真实密码、域名或 IP
- 配置文件模板只能放占位符
- 如发现安全漏洞，请私下联系而不要开公开 Issue
