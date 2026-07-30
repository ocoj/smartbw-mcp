# 变更日志

## [2.3.0] - 2026-07-30

### 🚀 公开发布准备

- **配置架构重构**: 统一运行时目录 `~/.config/bitwarden-mcp/`，移除 OpenClaw/Deep Code 路径绑定
- **SKILL.md 通用化**: 去平台绑定，OpenClaw/Deep Code 集成指南移至 `docs/integrations/`
- **安全加固**: `crypto_config.py` 写入 config.json 后显式 `chmod 0o600`；密钥派生版本统一为 v1
- **新增文档**: `docs/architecture.md`, `docs/dependencies.md`, `CONTRIBUTING.md`, `LICENSE`, `config.example.json`
- **三层配置泄露防护**: pre-commit hook + CI workflow + .gitignore 白名单
- **通用安装脚本**: `install.sh` 支持 systemd 和后台进程两种模式
- **内部文档归档**: 审计报告、旧版代码归档移至 `docs/dev/`（不入仓库）

### 🔧 Bug 修复

- **H-1**: `_get_client_ctx()` 上下文管理器自动关闭 socket，6 个工具统一使用
- **H-2**: 移除全部硬编码内网 URL，无配置时明确报错
- **M-7**: 8 处异常处理增加 `logger.exception()` 记录完整 traceback
- **M-8**: `smartbw_sync_cache` socket 操作增加 `finally` 关闭

## [2.2.7] - 2026-07-20

### 🔍 审计 & 文档对齐

- **CHANGELOG 清理**: 删除 v2.2.2 整节（`_recover` 死锁修复 — 未实际实现），清理 v2.2.1 中 F-01/F-02/F-06/部署同步等未实现条目，删除 v2.2.0 S-01（`_get_cached_client` — 未实际实现）
- **测试修复**:
  - `test_v22_audit_fixes.py` 重写，仅保留 F-05（DaemonClient 超时异常类型），移除验证未实现功能的 F-01/F-02 测试
  - `test_imports.py` 移除对已归档模块的引用（`bw_for_weak_ai`/`real_mcp_client`/`diagnose`），添加直接运行入口
  - 删除 `test_integration_real.py`（依赖已归档模块，无法运行）

### 🔧 Bug 修复

- **H-2**: `smartbw_sync_cache` 中 `BW_HOST` 从硬编码改为 `get_config().get("bw_host", ...)`，与其他路径一致
- **S-3**: `mcp_daemon.py` 模块导入不再向 `daemon.log` 写日志 — 文件日志初始化提取为 `_setup_file_logging()`，仅 daemon 启动时调用
- **M-4**: `mcp_raw.py` `_send_request` 异常捕获 `isinstance(e, (OSError, socket.error))` 覆盖 `builtins.ConnectionError`，修复从 daemon 抛出的连接异常无法触发重连的问题
- **M-1**: 双层熔断器阈值统一为 5（`smartbw_mcp_server.py` 外层 3→5，与 `mcp_raw.py` 内层一致）
- **F-05**: `DaemonClient.send_request()` 超时改为 `raise MCPTimeoutError`，确保熔断器正确捕获

### 🔒 安全

- `install_openclaw.sh` 提示文本中内网 URL 替换为通用示例
- `~/.config/bitwarden-mcp/config.json` 权限修正为 600

### 🧹 清理

- `pyproject.toml` 删除无用的 `[tool.setuptools.packages.find]` 配置（扁平模块布局不需要）

📄 `CHANGELOG.md`, `smartbw_mcp_server.py`, `mcp_daemon.py`, `mcp_raw.py`, `pyproject.toml`, `install_openclaw.sh`, `tests/` — 10 文件

## [2.2.6] - 2026-07-20

### 🔧 优雅升级机制 — 免 kill 重启 MCP Server

- **问题**: 代码更新后需手动 kill 旧 MCP server 进程，但 kill 后 Deep Code 不自动重连，导致工具不可用
- **方案**: 信号文件 `~/.smartbw-mcp/restart.signal` — MCP server 每次处理完请求后检查此文件，存在则优雅退出（`exit(0)`）
- **效果**: Deep Code 发现 stdio 进程正常退出后，下次调用自动 spawn 新进程，加载最新代码
- **使用**: 部署脚本更新代码后 `touch ~/.smartbw-mcp/restart.signal`，下次请求时自动完成升级

📄 `smartbw_mcp_server.py` — 1 文件

## [2.2.5] - 2026-07-20

### 🔧 smartbw_daemon_status UnboundLocalError 修复

- **根因**: `_handle_tools_call()` 函数内 `smartbw_sync_cache` 分支中包含冗余的 `from pathlib import Path`，Python 将其识别为整个函数的局部变量，导致 `smartbw_daemon_status` 分支中先于赋值使用 `Path` 时抛出 `UnboundLocalError`
- **修复**: 移除 `smartbw_sync_cache` 分支内的冗余 `import os` 和 `from pathlib import Path`（模块顶部已导入）

📄 `smartbw_mcp_server.py` — 1 文件

## [2.2.4] - 2026-06-26

### 🔧 缓存重建前 sync 确保数据最新

- **根因**: daemon session 可长期保持 "unlocked" 状态，但期间新增的组织金库条目不被返回。`_ensure_cache()` 重建缓存时直接 `list_items`，拿到过期数据导致新条目搜索不到
- **修复**: `_ensure_cache()` 缓存重建前调用 `bw sync`（调用 MCP `sync` tool）确保数据最新，失败静默降级
- **影响**: 缓存 TTL 300s，sync 最多每 300s 触发一次，增量拉取开销可忽略

📄 `smart_search.py` — 1 文件

## [2.2.3] - 2026-05-31

### 🔧 systemd Type=forking → Type=simple（根因修复）

- **根因**: daemon `--daemon` 模式用 `os.fork()` 后台化，但 systemd `Type=forking` + `PIDFile=` 无法正确追踪子进程 PID
  - 父进程 fork 后立即 exit → systemd 认为主进程退出
  - 子进程解锁 Vaultwarden 耗时 ~50s → systemd 在等 PID 文件 → 超时 → SIGKILL
  - 触发 Restart=on-failure → 死循环（今天 17 次重启，一次窗口内连续 14 次）
- **修复**: `Type=simple`，去掉 `--daemon` 参数和 `PIDFile=`，systemd 直接管理前台进程

📄 `smartbw-daemon.service` — 1 文件

## [2.2.1] - 2026-05-31

### 异常类型修复
- **F-05**: `DaemonClient.send_request()` 超时改为抛 `MCPTimeoutError`（而非内置 `TimeoutError`），确保 `_with_circuit` 熔断器正确计数

### 代码质量
- **F-03**: 删除 `bw_for_weak_ai.py` 未使用的 `from mcp_raw import RealMCPClient`
- **F-04**: 删除 `smartbw_mcp_server.py` 未使用的 `from mcp_raw import RealMCPClient`

---

## [2.2.0] - 2026-05-26

### 审计修复（15 项审计发现全修）
- **D-01**: `_diag_mcp_server()` 不再自动 `npm install`，改为提示手动安装
- **B-07**: `client_buffers` 增加 1MB 上限，防止恶意客户端 OOM
- **C-1**: 空缓存时重置 `_cache_time=0` 避免 TTL 空窗屏蔽后续搜索
- **C-2**: `list_items`/`get_item`/`get_password` 增加 `ConnectionError` 捕获
- **C-3**: API Key 分割增加 `len(parts) >= 3` 校验
- **C-4**: `bw_for_weak_ai.py` 的 `diagnose` 导入包裹 try/except
- **B-01**: `DaemonClient.connect()` 失败时显式 `sock.close()`
- **C-5**: 敏感信息日志截断（client_id: 20→12 字符, email 截断到 12 字符）
- **C-6**: `get_notes()` 空值返回 `None` 而非 `""`
- **C-7**: `_fuzzy_score` 增加最小查询长度保护（<3 字符降分到 0.50）
- **B-03**: 经复核确认 `_is_unlocked()` 已有 `timeout=10s`（假阳性）

---

## [2.1.0] - 2026-05-26

### Session 自愈
- **`mcp_daemon.py` 新增长期 session 管理**: 每 120s 通过 `bw status` 检测 session 有效性，过期自动 `auto_unlock` + 重启 MCP 子进程
- **`mcp_daemon.py` `MCPServerManager.start()`**: 支持可选 `bw_session` 参数替换旧 token

### 内存泄漏修复
- **`mcp_daemon.py` 进程组隔离**: Popen 添加 `start_new_session=True`，确保杀 MCP 子进程时所有 bw 孙子进程一并清理
- **`mcp_daemon.py` `MCPServerManager.stop()`**: 改 `terminate/kill` 为 `os.killpg`，杀整个进程组，防止僵尸 bw 子进程堆积

### 故障排查
- **SKILL.md**: 更新 session 过期诊断信息，明确 daemon 120s 自动恢复能力

---

## [2.0.0] - 2026-05-13

### 重大清理
- **移除 `bw_session` 虚参**: 所有公开函数签名删除 `bw_session` 参数（daemon 架构下从未生效），同步更新 `async_bw.py`
- **`RealMCPClient.__init__`**: 删除 `bw_session` 参数，daemon 自主管理 session
- **`SmartBitwardenMCP.__init__`**: 删除 `bw_session` 参数
- **`get_smart_mcp()` / `get_password_smart()`**: 删除 `bw_session` 参数

### Bug 修复
- **`bw_for_weak_ai.py` NameError**: 添加缺失的 `logger = logging.getLogger(__name__)`，修复 L135/L238 的 `logger.debug()` 调用
- **`real_mcp_client.py` 兼容包装**: 恢复缺失的兼容性 re-export 文件（被 `test_imports.py` 依赖）
- **`unlock.py` `_is_unlocked` 误判**: 改用 JSON 解析 `bw status` 输出，避免 `"status":"unauthenticated"` 被误判为已解锁

### 稳定性增强
- **`smart_search.py` `_do_fuzzy_search` 重试保护**: 二次 `list_items` 增加 try/except TimeoutError，防止 Vaultwarden 完全不可达时裸奔
- **`mcp_raw.py` `_send_request` 异常处理**: 添加 `isinstance` 类型检查（BrokenPipeError/ConnectionResetError 等），减少对字符串匹配的依赖
- **`smartbw_mcp_server.py` 健康检查**: 用公开方法 `ping()` 替代内部 `_send_request`，走完整熔断器路径
- **`mcp_daemon.py` 线程管理**: 健康检查线程存入 `self._health_thread`，cleanup 时 join 等待优雅退出

### 性能优化
- **`smartbw_mcp_server.py` import 提升**: `SmartBitwardenMCP`/`RealMCPClient` 移至模块顶部，消除每次请求的 import 开销
- **`smart_search.py` 排序优化**: 删除冗余的 `scored.sort()`，合并后只排一次
- **`mcp_daemon.py`**: `import random` 移至模块顶

### 代码质量
- **`config.py`**: `setup_logging()` 加 `if __name__ == "__main__"` 守卫，消除 import 副作用
- **`mcp_daemon.py`**: 导入自定义异常（`MCPTimeoutError`），与 `mcp_raw.py` 异常类型一致
- **`unlock.py`**: 添加 `import json`

### 破坏性变更
- ⚠️ 所有公开函数签名移除 `bw_session` 参数（v1.x 调用代码需删除该实参）
- ⚠️ `SmartBitwardenMCP(bw_session=...)` → `SmartBitwardenMCP()`
- ⚠️ `get_smart_mcp(bw_session=...)` → `get_smart_mcp()`

---

## 2026-05-02 — 代码审计 & 质检体系

### 🔍 全量代码审计
- 扫描全部10个.py文件，3476行代码
- 死代码: 0个（async_bw为可选模块）
- 安装/发布同步: ✅ 0差异

### 🛡️ 质检体系
- **ACTIVATION.yaml**: 12个功能登记  
- **verify_activation.py**: 自动质检（API调用链+守护进程+导入+同步）

### ✅ 审计结果
- 核心API 12/12函数全部有调用链
- smartbw-daemon 运行正常
- 8/8模块导入正常

---


所有对 Smart Bitwarden MCP 项目的显著更改都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
并且本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.7.0] - 2026-04-29

### 安全加固
- **DaemonClient 线程安全**: `send_request` 添加 `threading.Lock` 保护收发原子性，防止多线程并发导致 JSON 响应错乱
- **Unix Socket 权限竞态修复**: `bind()` 前设置 `umask(0o077)`，确保 socket 文件创建时就是 `0o600` 权限，消除 bind→chmod 之间的时间窗口
- **bw_for_weak_ai 信息泄露**: 字段名/字段值相关的 `print()` 改为 `logger.debug()`，避免密码内容泄露到 stdout
- **移除 `_with_stdout_suppressed`**: 重新设计后不再需要绕过 stdout，相关代码 + `import io` 已清理

### 稳定性增强
- **熔断器 (Circuit Breaker)**: `RealMCPClient` 新增连续失败跟踪，5 次失败后进入 30s 冷却期，防止 Vaultwarden 不可用时 7 分钟无限重试
- **Daemon 自愈**: `DaemonServer` 新增加 `_health_check_loop`（60s 间隔），MCP 子进程死亡时自动重启
- **Daemon 启动重试**: `start()` 中 `sleep(3)` 硬编码等待改为 15 次 × 0.5s 连接重试循环，消除竞态条件
- **DaemonClient 逐 chunk 超时**: `send_request` 中 `recv` 改为每次 `settimeout(min(5s, remaining))`，避免全局 timeout 卡死

### 性能优化
- **smartbw_mcp_server 请求去重**: 工具处理器统一使用 `SmartBitwardenMCP(use_daemon=True)` + `try/finally close()`，消除每次新建进程的开销
- **客户端缓存熔断**: `_get_cached_client()` 新增快速失败路径（3 次错误后 30s 冷却），避免重复重连
- **移除 bw_for_weak_ai 回退**: 工具处理器不再创建第二个客户端，单一 daemon 路径处理所有请求

## [1.6.0] - 2026-04-28

### 架构优化
- **自动解锁逻辑独立**：`mcp_raw.py` 中 ~170 行的 `_auto_unlock()` 提取为独立 `unlock.py` 模块，职责单一化
- **消除重复代码**：`smart_search.py` 中 `list_all()` 改为委托 `list_all_items()`，`fuzzy_search()` 改为委托 `search_items()`，减少 ~80 行重复逻辑
- **消除常量重复定义**：`config.py` 底部的 `DEFAULT_TIMEOUT` / `FUZZY_THRESHOLD` / `AUTO_UNLOCK` / `MAX_AUTO_UNLOCK_ATTEMPTS` 与顶部重复，已移除底部定义
- **`bw_for_weak_ai.py` 导入简化**：使用固定 `sys.path.insert` 替代 `try/except ImportError` 冗余分支，并将 `diagnose/setup` 导入提前到文件顶部

### Bug 修复
- **硬编码路径**：`config.py` 中 `"/home/<user>/..."` 替换为 `os.path.expanduser("~/.local/...")`
- **缺失 `import sys`**：`smart_search.py` 中多处 `sys.exit()` 和 `sys.stderr` 引用但未显式 `import sys`
- **`config.py` 未使用 import**：移除未使用的 `import sys` 和 `import re`（已替换到正确位置）

### 安全审计
- 确认所有 `subprocess.run()` 调用均使用安全模式（无 `shell=True`，无用户输入注入）
- 确认无密码泄露到日志或 stdout
- 确认文件路径操作使用 Path 而非字符串拼接

## [1.5.0] - 2026-04-28

### 重构
- **模块化拆分**：`real_mcp_client.py` (1531行) 拆分为 5 个独立模块
  - `config.py` — 配置加载 + 自动发现 MCP 路径
  - `models.py` — 数据类 + 异常定义
  - `mcp_raw.py` — RealMCPClient 原始 MCP 通信层
  - `smart_search.py` — SmartBitwardenMCP 智能搜索层
  - `real_mcp_client.py` — 兼容性包装（20行 re-export）
- **入口统一**：删除 `smart_bw.py`（667行），功能合并到 `bw_for_weak_ai.py` + `diagnose.py`
- **项目规范**：`projects/` 和 `skills/` 双向同步完整代码

### 新增
- **搜索索引加速**：`_name_index` 精确/前缀匹配快速路径，大库搜索性能提升
- **缓存优化**：TTL 从 30s 提升至 300s，减少不必要的全量拉取
- **MCP 自动发现**：新增 `npm ls -g` + `which` 检测，替代手动配置路径
- **自动化测试**：10 个冒烟测试（`tests/test_imports.py`）

### 移除
- `smart_bw.py`（功能已合并）

## [1.4.1] - 2026-04-28

### 修复
- **自动解锁失败**：`~/.config/bitwarden-mcp/config.json` 缺少 `email` 和 `client_id`，导致 `_auto_unlock()` 无法登录。已补全必需字段
- **MCP 超时**：`connection_timeout_seconds` 从 10 提升至 15 秒，避免解锁流程超时
- **环境变量缺失**：`.secrets/.env` 新增 `BW_EMAIL` 和 `BW_CLIENTSECRET` 变量

### 文档更新
- SKILL.md：更新配置说明，补充 `email`/`client_id` 必需字段
- INSTALL.md：更新配置文件示例，增加故障排查章节
- README.md：明确自动解锁所需的配置要求

### 架构说明
- 明确 smartbw-mcp 与 OpenClaw 原生 MCP (`bitwarden__*`) 是**两个独立模块**，各自管理 session
- OpenClaw MCP 使用 `bw-mcp-wrapper.sh` 启动时动态获取 BW_SESSION
- smartbw-mcp 通过 `_auto_unlock()` 独立完成登录+解锁

---

## [1.4.0] - 2026-04-17

### 新增功能
- **智能自动重试机制**：首次搜索无结果时自动清除缓存重试一次，解决新增项目搜索延迟问题
- **用户可见提示**：重试时显示“正在刷新缓存后重试，请稍候”，防止 AI 误判程序卡死
- **创建后自动刷新缓存**：`create_pwd()` 成功创建项目后自动清除缓存，新项目可立即搜索
- **新增实用函数**：`clear_cache()` 手动清除缓存，`check_connection()` 检查连接状态
- **自定义字段搜索增强**：模糊搜索现在包含自定义字段名和值的匹配
- **隐藏字段值修复**：`hidden` 类型字段现在返回真实值而非占位符

### 性能优化
- **30秒缓存机制**：大幅提升重复搜索性能，减少 API 调用
- **自动过期**：缓存30秒后自动刷新
- **智能重试逻辑**：只在首次搜索无结果且缓存存在时重试，避免性能问题

### 错误修复
- **字段名大小写敏感问题**：字段名比较现在不区分大小写
- **自动解锁返回值类型**：修复 `_auto_unlock()` 返回值类型提示与实际不一致问题
- **模糊搜索阈值优化**：提高匹配阈值至 0.5，减少无关结果

### 文档更新
- **SKILL.md 全面更新**：新增最佳实践、性能优化、新增项目解决方案等章节
- **完整函数列表**：更新所有可用函数说明，包括新增的 `clear_cache()` 和 `check_connection()`

## [1.3.0] - 2026-04-16

### 新增功能
- **AI 可调用安装函数**：`setup_with_config()` 支持程序化调用
- **交互/非交互分离**：AI 调用无交互，人类运行脚本才等待输入
- **敏感信息安全存储**：主密码/API Key 存储在 `~/.config/bitwarden-mcp/.env`
- **API Key 认证支持**：优先使用 API Key 登录（兼容性最强，支持所有 2FA）
- **认证优先级**：API Key + 主密码 > 用户名 + 主密码
- **代码脱敏**：所有真实信息已替换为占位符示例

### 认证方式说明
- **API Key + 主密码**：兼容性最强，支持 FIDO2/Duo 等不支持的 2FA
- **用户名 + 主密码**：可能因 2FA 验证方式不被 CLI 支持而失败

### 脱敏信息（所有真实数据已替换为示例）
- 服务器地址 → `https://vaultwarden.example.com`
- 邮箱 → `user@example.com`
- 主密码 → `YourMasterPassword123`
- API Key → `user.clientId.clientSecret`

### 集成到 OpenClaw
- 更新 `bitwarden-mcp-guide/SKILL.md`，推荐使用 smart-bitwarden-mcp
- 更新 `openclaw-bitwarden/SKILL.md`，推荐使用 smart-bitwarden-mcp
- 其他 AI 模型现在可以通过读取 SKILL.md 了解如何使用

### 自动化增强
- **前置锁定检测**：所有操作前自动检测金库状态
- **自动解锁重试**：锁定时自动解锁并重试操作
- **Session 自动管理**：自动保存和恢复 session
- **一键诊断工具**：自动检查所有依赖和配置
- **一键配置工具**：自动创建配置文件

### Python API
- `get_password(search_term)` - 获取密码
- `list_all()` - 列出所有项目
- `search_items(query)` - 搜索项目
- `get_item(item_id)` - 获取项目详情
- `create(name, ...)` - 创建项目
- `update(item_id, ...)` - 更新项目
- `delete(item_id)` - 删除项目

---

## [1.1.0] - 2026-04-16

### 修复
- **超时保护**：添加 10 秒超时机制，防止无限卡死
- **进程健康检查**：添加 `ping` 命令和 `health_check()` 方法
- **搜索阈值优化**：从 0.3 提升到 0.5，减少无关结果
- **多协议版本兼容**：尝试多个 protocolVersion 找兼容版本
- **进程回收机制**：自动清理僵尸进程
- **超时异常导出**：在 bw_for_weak_ai.py 中正确处理 TimeoutError
- **Locked 前置检查**：list/get 等操作前先检测金库是否锁定，快速失败不卡死

### 新增
- **ping 命令**：CLI 新增健康检查命令
- **timeout 参数**：所有 CLI 命令支持 `--timeout` 参数
- **LockedError 异常**：专门的金库锁定异常类型
- **自动解锁**：检测到金库锁定时自动调用 `bw unlock` 获取新 token 并重试操作

---

## [1.0.0] - 2026-04-16

### 新增
- **真正的 Bitwarden MCP 封装**：通过 stdio 与官方 `@bitwarden/mcp-server` 通信
- **智能模糊搜索**：6种打分策略，不区分大小写，自动容错
- **自动重试机制**：第一次无结果自动重试，自动刷新 session
- **简化配置系统**：支持环境变量、配置文件、默认值三级配置
- **弱 AI 友好接口**：`get_pwd()` 一个函数搞定所有
- **完整功能集**：get, search, list, test, ping 等操作
- **高性能设计**：直接 MCP 通信，无进程开销，比 CLI 快 10-15 倍
- **标准化技能包**：符合 OpenClaw 技能规范，可在社区发布

### 技术特性
- 使用正确的 `tools/call` MCP 协议调用方式
- 支持环境变量配置（`BW_HOST`, `MCP_SERVER_PATH`, `BW_SESSION`）
- 支持配置文件（`~/.config/bitwarden-mcp/config.json`）
- 自动检测常见 MCP 服务器路径
- 详细的错误消息和故障排除指南
- 完整的文档和示例代码

### 文档
- README.md：项目概述和快速开始
- SKILL.md：完整的技能文档和使用指南
- INSTALL.md：详细安装和配置说明
- CONTRIBUTING.md：贡献指南
- LICENSE：MIT 许可证

## [0.1.0] - 2026-04-15

### 新增
- 初始版本概念验证
- 基本的 MCP 通信框架
- 简单的模糊搜索实现

### 技术特性
- 初步的 MCP 协议支持
- 基础配置管理
- 示例代码和测试

---

## 版本计划

### 计划中的功能
- [ ] 批量操作优化
- [ ] 缓存机制
- [ ] 图形界面
- [ ] 更多搜索策略
- [ ] 插件系统

### 已知问题
- 首次连接可能需要手动配置 MCP 服务器路径
- 某些网络环境下连接可能超时
- 大规模密码库搜索可能较慢

---

## 版本号说明

- **主版本号**：不兼容的 API 修改
- **次版本号**：向下兼容的功能新增  
- **修订号**：向下兼容的问题修复

## 更新日志格式

每个版本应包含以下部分：

- `新增`：新功能
- `更改`：现有功能的变更
- `弃用`：即将移除的功能
- `移除`：已移除的功能
- `修复`：问题修复
- `安全`：安全性更新