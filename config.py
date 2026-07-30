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
from typing import Dict

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

# 自动解锁
AUTO_UNLOCK = os.environ.get("SMARTBW_AUTO_UNLOCK", "1") != "0"
MAX_AUTO_UNLOCK_ATTEMPTS = int(os.environ.get("SMARTBW_MAX_UNLOCK_ATTEMPTS", "3"))


# ============================================================================
# 配置 - 通过环境变量或配置文件
# ============================================================================

def _get_runtime_dir() -> Path:
    """运行时配置目录。默认 ~/.config/bitwarden-mcp/，可通过 SMARTBW_CONFIG_DIR 覆盖。"""
    custom = os.environ.get("SMARTBW_CONFIG_DIR", "")
    if custom:
        return Path(custom)
    return Path.home() / ".config" / "bitwarden-mcp"


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


def get_config() -> Dict[str, str]:
    """获取配置,优先级:环境变量 > .env 文件 > config.json。缺失关键字段时不提供默认占位符。"""
    config: Dict[str, str] = {}
    runtime_dir = _get_runtime_dir()

    # 1. .env 文件（运行时目录，可选）
    _load_dotenv(runtime_dir / ".env")

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
    config_file = runtime_dir / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                file_config = json.load(f)
            # 启动时解密/加密 config.json 敏感字段
            try:
                from crypto_config import process_config_on_startup
                decrypted = process_config_on_startup()
                if decrypted:
                    file_config.update(decrypted)
            except ImportError:
                pass
            # 补全模式: 只填充 config 中尚未设置的值
            for key, value in file_config.items():
                if key not in config or not config[key]:
                    config[key] = value
        except (json.JSONDecodeError, OSError):
            pass

    # 4. MCP Server 路径自动发现
    if not config.get("mcp_server_path"):
        config["mcp_server_path"] = _find_mcp_path()

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

    handlers: list[logging.Handler] = [logging.StreamHandler()]
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
