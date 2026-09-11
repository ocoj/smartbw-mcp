"""
配置加载模块

负责:
- 环境变量/配置文件的加载
- 日志配置
- 全局常量定义

配置优先级: 环境变量 > .env 文件 > config.json

运行时配置目录: ~/.config/bitwarden-mcp/ (可通过 SMARTBW_CONFIG_DIR 覆盖)
"""
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from paths import runtime_dir

# ============================================================================
# 全局常量（可通过环境变量覆盖）
# ============================================================================

# MCP 通信超时（秒）
DEFAULT_TIMEOUT = int(os.environ.get("SMARTBW_MCP_TIMEOUT", "30"))

# CLI 操作超时（bw unlock/login/status）
CLI_STATUS_TIMEOUT = int(os.environ.get("SMARTBW_CLI_STATUS_TIMEOUT", "10"))
CLI_LOGIN_TIMEOUT = int(os.environ.get("SMARTBW_CLI_LOGIN_TIMEOUT", "15"))
CLI_UNLOCK_TIMEOUT = int(os.environ.get("SMARTBW_CLI_UNLOCK_TIMEOUT", "15"))
CLI_DISCOVERY_TIMEOUT = int(os.environ.get("SMARTBW_CLI_DISCOVERY_TIMEOUT", "10"))

# 模糊搜索
FUZZY_THRESHOLD = float(os.environ.get("SMARTBW_FUZZY_THRESHOLD", "0.5"))

# 项目缓存（长驻实例跨调用复用）
# 后端多为个人自建 Vaultwarden，TTL 必须短：太长会出现"条目已更新但查到旧值"。
CACHE_TTL = int(os.environ.get("SMARTBW_CACHE_TTL", "15"))
# 无结果 / 结果可疑时触发强制刷新的最小间隔，避免一次查询白跑两遍、失败后连环重试
CACHE_REFRESH_MIN_AGE = int(os.environ.get("SMARTBW_CACHE_REFRESH_MIN_AGE", "5"))
# 最高分低于此值视为"结果可疑"，触发一次强制刷新重查。
# 必须 > FUZZY_THRESHOLD：低于阈值的候选在评分阶段就被丢弃了，
# 所以阈值等于 FUZZY_THRESHOLD 时这条分支永远不会触发（等于没用）。
CACHE_SUSPICIOUS_SCORE = float(os.environ.get("SMARTBW_CACHE_SUSPICIOUS_SCORE", "0.6"))

# 自动解锁
AUTO_UNLOCK = os.environ.get("SMARTBW_AUTO_UNLOCK", "1") != "0"
MAX_AUTO_UNLOCK_ATTEMPTS = int(os.environ.get("SMARTBW_MAX_UNLOCK_ATTEMPTS", "3"))


# ============================================================================
# 配置 - 通过环境变量或配置文件
# ============================================================================

def _get_runtime_dir() -> Path:
    """运行时配置目录。默认 ~/.config/bitwarden-mcp/，可通过 SMARTBW_CONFIG_DIR 覆盖（支持 ~）。"""
    return runtime_dir()


def _resolve_runtime_dir() -> Path:
    """解析运行时目录，并加载该目录的 .env。

    SMARTBW_CONFIG_DIR 既可能来自真实环境变量，也可能写在默认目录的 .env 里，
    因此做两趟：先按初值加载 .env；若其中把目标目录改了，再重算并加载新目录的 .env。
    """
    resolved = _get_runtime_dir()
    _load_dotenv(resolved / ".env")
    recheck = _get_runtime_dir()
    if recheck != resolved:
        resolved = recheck
        _load_dotenv(resolved / ".env")
    return resolved


def _load_dotenv(dotenv_path: Path) -> None:
    """加载 .env 格式文件，将未设置的环境变量注入 os.environ。"""
    if not dotenv_path.exists():
        return
    try:
        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_config_cache: Optional[Dict[str, str]] = None


def get_config(refresh: bool = False) -> Dict[str, str]:
    """获取配置,优先级:环境变量 > .env 文件 > config.json。缺失关键字段时不提供默认占位符。

    进程内缓存：配置在进程生命周期内不变，而每次解析都要读文件、跑 crypto_config
    （它会读写 config.json），且 MCP 路径自动发现会起 npm/which 子进程 —— 只应发生一次。
    需要强制重读时传 refresh=True。
    """
    global _config_cache
    if _config_cache is not None and not refresh:
        return _config_cache

    config: Dict[str, str] = {}
    # 1. .env 文件（运行时目录，可选）—— 同时解析 SMARTBW_CONFIG_DIR 的两趟语义
    cfg_dir = _resolve_runtime_dir()

    # 2. 环境变量（优先级最高）
    config["bw_host"] = os.environ.get("BW_HOST", "")
    config["mcp_server_path"] = os.environ.get(
        "MCP_SERVER_PATH",
        os.environ.get("BITWARDEN_MCP_SERVER_PATH", "")
    )
    config["master_password"] = os.environ.get("BW_MASTER_PASSWORD", "")
    config["email"] = os.environ.get("BW_EMAIL", "")
    config["client_id"] = os.environ.get("BW_CLIENTID", "")
    config["client_secret"] = os.environ.get("BW_CLIENTSECRET", "")
    config["api_key"] = os.environ.get("BW_API_KEY", "")

    # 3. 配置文件（补充环境变量未设置的值）
    config_file = cfg_dir / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                file_config = json.load(f)
            # 启动时解密/加密 config.json 敏感字段
            encrypted_prefix = "!enc:v1:"
            try:
                from crypto_config import ENCRYPTED_PREFIX, process_config_on_startup
                encrypted_prefix = ENCRYPTED_PREFIX
                decrypted = process_config_on_startup()
                if decrypted:
                    file_config.update(decrypted)
            except ImportError:
                pass  # 未安装 cryptography 时 config.json 不会存在密文
            # 补全模式: 只填充 config 中尚未设置的值
            for key, value in file_config.items():
                # 解密失败的密文绝不能降级当明文使用（否则会被当成主密码去登录）
                if isinstance(value, str) and value.startswith(encrypted_prefix):
                    logger.error(
                        "配置项 %s 仍为密文（解密失败），已跳过；请检查机器指纹是否变更，"
                        "或执行 python3 -m mcp_daemon --reinit 重新初始化", key
                    )
                    continue
                if key not in config or not config[key]:
                    config[key] = value
        except (json.JSONDecodeError, OSError):
            pass

    # 4. MCP Server 路径自动发现
    if not config.get("mcp_server_path"):
        config["mcp_server_path"] = _find_mcp_path()

    _config_cache = config
    return config


def _find_mcp_path() -> str:
    """自动发现 MCP Server 路径，优先级：npm全局→常见路径→which→空"""
    # 1. 尝试 npm ls -g 查找
    try:
        result = subprocess.run(
            ["npm", "ls", "-g", "@bitwarden/mcp-server", "--depth=0"],
            capture_output=True, text=True, timeout=CLI_DISCOVERY_TIMEOUT
        )
        for line in result.stdout.split('\n'):
            if '@bitwarden/mcp-server@' in line:
                match = re.search(r'->\s+(.+?)(?:\n|$)', line)
                if match:
                    candidate = Path(match.group(1).strip()).resolve()
                    dist_path = candidate / "dist" / "index.js"
                    if dist_path.exists():
                        return str(dist_path)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 2. 尝试 which（符号链接）
    try:
        result = subprocess.run(
            ["which", "bitwarden-mcp-server"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 3. 常见路径
    common_paths = [
        "/usr/local/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        "/usr/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        "/opt/homebrew/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        os.path.expanduser("~/.npm-global/lib/node_modules/@bitwarden/mcp-server/dist/index.js"),
        os.path.expanduser("~/.local/lib/node_modules/@bitwarden/mcp-server/dist/index.js"),
    ]
    for path in common_paths:
        if Path(path).exists():
            return path

    return ""


# ============================================================================
# 日志配置
# ============================================================================


def setup_logging():
    """设置日志"""
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    log_file = os.environ.get("LOG_FILE")

    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file))
        except Exception:
            pass

    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers
    )


# 向后兼容：`from config import CONFIG` 触发惰性加载，避免 import 时即执行 npm/which。
def __getattr__(name):
    if name == "CONFIG":
        return get_config()
    raise AttributeError(f"module 'config' has no attribute '{name}'")


# 仅当作为入口脚本直接运行时才配置日志，import 时不产生副作用
if __name__ == "__main__":
    setup_logging()

logger = logging.getLogger(__name__)
