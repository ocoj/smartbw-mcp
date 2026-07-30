#!/usr/bin/env python3
"""
Smart Bitwarden MCP 一键安装脚本

两种使用方式：
1. AI/程序化调用（无交互）：
   from setup import setup_with_config
   setup_with_config(bw_host="...", email="...", master_pw="...", api_key="...")

2. 人类交互式运行：
   python3 setup.py

敏感信息存储规范：
- 主密码、API Key → ~/.config/bitwarden-mcp/.env
- 服务器地址、邮箱 → 同上或 ~/.config/bitwarden-mcp/
"""
import getpass
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

# 统一 timeout 常量
try:
    from config import CLI_STATUS_TIMEOUT
except ImportError:
    CLI_STATUS_TIMEOUT = 10

# ============================================================================
# 颜色输出
# ============================================================================

class Colors:
    GREEN = '\033[0;32m'
    RED = '\033[0;31m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    CYAN = '\033[0;36m'
    NC = '\033[0m'

def log_info(msg):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")

def log_success(msg):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.NC} {msg}")

def log_warning(msg):
    print(f"{Colors.YELLOW}[WARNING]{Colors.NC} {msg}")

def log_error(msg):
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}")

def log_step(step, total, msg):
    print(f"{Colors.BLUE}[{step}/{total}]{Colors.NC} {msg}")

# ============================================================================
# 敏感信息存储路径
# ============================================================================

def get_secrets_dir() -> Path:
    """获取 OpenClaw 敏感信息目录"""
    return Path.home() / ".config" / "bitwarden-mcp"

def get_secrets_env_file() -> Path:
    """获取敏感信息配置文件"""
    return get_secrets_dir() / ".env"

def get_config_dir() -> Path:
    """获取一般配置目录"""
    return Path.home() / ".config" / "bitwarden-mcp"

# ============================================================================
# 敏感信息读写
# ============================================================================

def read_secrets() -> Dict[str, str]:
    """从 .secrets/.env 读取敏感信息"""
    secrets_file = get_secrets_env_file()
    config = {}
    if secrets_file.exists():
        try:
            content = secrets_file.read_text()
            for line in content.split('\n'):
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    v = v.strip('"\'')
                    config[k] = v
        except Exception:
            pass
    return config

def write_secrets(config: Dict[str, str]):
    """写入敏感信息到 .secrets/.env"""
    secrets_dir = get_secrets_dir()
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secrets_file = get_secrets_env_file()

    # 读取现有内容（保留非 Vaultwarden 的配置）
    existing = {}
    if secrets_file.exists():
        try:
            content = secrets_file.read_text()
            for line in content.split('\n'):
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    if not k.startswith('VW_') and not k.startswith('BW_'):
                        existing[k] = v.strip('"\'')
        except Exception:
            pass

    # 合并配置
    existing.update(config)

    # 写入文件
    lines = [f'{k}="{v}"' for k, v in existing.items()]
    secrets_file.write_text('\n'.join(lines) + '\n')
    os.chmod(secrets_file, 0o600)
    log_success(f"敏感信息已保存到: {secrets_file}")

def mask_sensitive(value: str) -> str:
    """脱敏显示"""
    if not value or len(value) < 4:
        return "***"
    return value[:2] + "***" + value[-2:]

# ============================================================================
# 交互式安装引导
# ============================================================================

def first_time_setup_wizard() -> Dict[str, str]:
    """首次安装引导 - 必须由用户提供敏感信息"""
    print()
    print("=" * 60)
    print(f"{Colors.CYAN}首次安装引导{Colors.NC} - 需要您提供以下信息")
    print("=" * 60)
    print()

    config = {}

    # 1. 服务器地址
    print("1. Vaultwarden 服务器地址")
    print("   例如: https://vaultwarden.example.com")
    while True:
        bw_host = input(f"   请输入服务器地址: ").strip()
        if not bw_host:
            log_warning("服务器地址不能为空")
            continue
        if not bw_host.startswith(('http://', 'https://')):
            log_warning("地址必须以 http:// 或 https:// 开头")
            continue
        config['BW_HOST'] = bw_host
        break

    # 2. 邮箱
    print()
    print("2. 登录邮箱")
    while True:
        email = input(f"   请输入邮箱: ").strip()
        if not email or '@' not in email:
            log_warning("请输入有效的邮箱地址")
            continue
        config['BW_EMAIL'] = email
        break

    # 3. 主密码
    print()
    print("3. Vaultwarden 主密码")
    print("   ⚠️  此密码将安全存储，用于自动解锁")
    while True:
        master_pw = getpass.getpass("   请输入主密码: ").strip()
        if not master_pw:
            log_warning("主密码不能为空")
            continue
        confirm = getpass.getpass("   请再次输入确认: ").strip()
        if master_pw != confirm:
            log_warning("两次输入不一致，请重试")
            continue
        config['BW_MASTER_PASSWORD'] = master_pw
        break

    # 4. API Key（可选，但推荐，用于兼容 2FA）
    print()
    print("4. API Key（可选，但推荐）")
    print("   用途: 支持 FIDO2/Duo 等不支持的 2FA 验证方式")
    print("   获取: Vaultwarden Web UI -> 设置 -> API Key")
    print("   格式: user.clientId.clientSecret（Bitwarden 官方格式）")
    print("   或:   仅 clientId（Vaultwarden 简化格式）")
    api_key = input("   直接回车跳过: ").strip()
    if api_key:
        config['BW_API_KEY'] = api_key

    print()
    print("=" * 60)
    confirm = input(f"确认保存配置? (y/n): ").strip().lower()
    if confirm != 'y':
        print("已取消")
        return {}

    return config

# ============================================================================
# 系统检测
# ============================================================================

def get_system_info():
    return {
        "os": platform.system(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "arch": platform.machine(),
    }

def check_command(cmd: str) -> Optional[str]:
    """检查命令是否存在"""
    try:
        result = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=CLI_STATUS_TIMEOUT)
        if result.returncode == 0:
            return result.stdout.strip().split('\n')[0]
    except Exception:
        pass
    return None

def check_python() -> bool:
    """检查 Python"""
    v = sys.version_info
    if v.major >= 3 and v.minor >= 8:
        log_success(f"Python: {v.major}.{v.minor}.{v.micro}")
        return True
    log_error(f"Python 版本过低: {v.major}.{v.minor}.{v.micro} (需要 3.8+)")
    return False

def check_node() -> bool:
    """检查 Node.js"""
    if check_command("node"):
        log_success(f"Node.js: {check_command('node')}")
        return True
    log_error("Node.js 未安装")
    return False

def check_npm() -> bool:
    """检查 npm"""
    if check_command("npm"):
        log_success(f"npm: {check_command('npm')}")
        return True
    log_error("npm 未安装")
    return False

def check_bw_cli() -> bool:
    """检查 bw CLI"""
    if check_command("bw"):
        log_success(f"bw CLI: {check_command('bw')}")
        return True
    log_error("bw CLI 未安装")
    return False

def check_mcp_server() -> Optional[str]:
    """检查 MCP Server"""
    common_paths = [
        "/usr/local/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        "/usr/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        str(Path.home() / ".npm-global/lib/node_modules/@bitwarden/mcp-server/dist/index.js"),
        str(Path.home() / ".local/lib/node_modules/@bitwarden/mcp-server/dist/index.js"),
    ]
    for path in common_paths:
        if Path(path).exists():
            log_success(f"MCP Server: {path}")
            return str(path)
    log_warning("MCP Server 未安装")
    return None

def install_bw_cli() -> bool:
    """安装 bw CLI"""
    log_info("正在安装 bw CLI...")
    try:
        result = subprocess.run(["npm", "install", "-g", "@bitwarden/cli"],
                              capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            log_success("bw CLI 安装成功")
            return True
        log_error(f"安装失败: {result.stderr[:200]}")
    except Exception as e:
        log_error(f"安装失败: {e}")
    return False

def install_mcp_server() -> bool:
    """安装 MCP Server"""
    log_info("正在安装 @bitwarden/mcp-server...")
    try:
        result = subprocess.run(["npm", "install", "-g", "@bitwarden/mcp-server"],
                              capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            log_success("@bitwarden/mcp-server 安装成功")
            return True
        log_error(f"安装失败: {result.stderr[:200]}")
    except Exception as e:
        log_error(f"安装失败: {e}")
    return False

# ============================================================================
# 配置检测
# ============================================================================

def detect_existing_config() -> Dict[str, str]:
    """检测现有配置"""
    config = {}

    # 1. 从 .secrets/.env 读取（敏感信息）
    secrets = read_secrets()
    if secrets:
        if secrets.get('VW_HOST') or secrets.get('BW_HOST'):
            config['bw_host'] = secrets.get('VW_HOST') or secrets.get('BW_HOST')
        if secrets.get('VW_EMAIL') or secrets.get('BW_EMAIL'):
            config['email'] = secrets.get('VW_EMAIL') or secrets.get('BW_EMAIL')
        if secrets.get('VW_MASTER_PW') or secrets.get('BW_MASTER_PASSWORD'):
            config['master_pw'] = secrets.get('VW_MASTER_PW') or secrets.get('BW_MASTER_PASSWORD')
        if secrets.get('VW_API_KEY') or secrets.get('BW_API_KEY'):
            config['api_key'] = secrets.get('VW_API_KEY') or secrets.get('BW_API_KEY')
        if config:
            log_info("从 .secrets/.env 检测到现有配置")

    # 2. 环境变量（覆盖上面配置）
    for key, env_key in [('bw_host', 'BW_HOST'), ('email', 'BW_EMAIL'),
                          ('master_pw', 'BW_MASTER_PASSWORD'), ('api_key', 'BW_API_KEY')]:
        val = os.environ.get(env_key)
        if val:
            config[key] = val

    # 3. 其他配置文件
    other_paths = [
        Path.home() / ".config" / "bitwarden-mcp" / "config.json",
        Path.home() / ".config" / "bitwarden-mcp" / ".env",
        Path.home() / ".env",
    ]
    for p in other_paths:
        if not p.exists():
            continue
        try:
            if p.suffix == ".json":
                data = json.loads(p.read_text())
                if not config.get('bw_host'): config['bw_host'] = data.get('bw_host', '')
                if not config.get('email'): config['email'] = data.get('email', '')
                if not config.get('master_pw'): config['master_pw'] = data.get('master_password', '')
                if not config.get('api_key'): config['api_key'] = data.get('api_key', '')
            else:
                content = p.read_text()
                for line in content.split('\n'):
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        v = v.strip('"\'')
                        if k == 'BW_HOST' and not config.get('bw_host'): config['bw_host'] = v
                        if k == 'BW_EMAIL' and not config.get('email'): config['email'] = v
                        if k == 'BW_MASTER_PASSWORD' and not config.get('master_pw'): config['master_pw'] = v
                        if k == 'BW_API_KEY' and not config.get('api_key'): config['api_key'] = v
        except Exception:
            pass

    return config

def check_bw_status(bw_host: str) -> str:
    """检查 bw 登录状态"""
    try:
        env = os.environ.copy()
        if bw_host:
            env["BW_HOST"] = bw_host
        result = subprocess.run(["bw", "status"], capture_output=True, text=True, timeout=CLI_STATUS_TIMEOUT, env=env)
        output = result.stdout.lower()
        if '"unlocked"' in output:
            return "unlocked"
        elif '"locked"' in output:
            return "locked"
        elif "not logged in" in output:
            return "not_logged_in"
    except Exception:
        pass
    return "unknown"

# ============================================================================
# 配置存储
# ============================================================================

def save_config(config: Dict[str, str], mcp_path: str):
    """保存配置到正确位置"""
    # 1. 保存敏感信息到 .secrets/.env
    secrets = {}
    if config.get('bw_host'):
        secrets['BW_HOST'] = config['bw_host']
    if config.get('email'):
        secrets['BW_EMAIL'] = config['email']
    if config.get('master_pw'):
        secrets['BW_MASTER_PASSWORD'] = config['master_pw']
    if config.get('api_key'):
        secrets['BW_API_KEY'] = config['api_key']

    if secrets:
        write_secrets(secrets)

    # 2. 保存非敏感配置到 .config/bitwarden-mcp/
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    public_config = {
        "bw_host": config.get('bw_host', ''),
        "mcp_server_path": mcp_path,
        "email": config.get('email', ''),
        "session_file": str(config_dir / "session.token"),
        "search_settings": {
            "fuzzy_threshold": 0.5,
            "max_results": 5,
            "max_retries": 2,
        },
        "performance": {
            "connection_timeout_seconds": 15,
        },
    }

    config_file = config_dir / "config.json"
    with open(config_file, "w") as f:
        json.dump(public_config, f, indent=2)
    log_success(f"配置文件: {config_file}")

# ============================================================================
# 测试连接
# ============================================================================

def test_connection(config: Dict[str, str], mcp_path: str) -> bool:
    """测试连接"""
    log_info("测试连接...")
    try:
        env = os.environ.copy()
        if config.get('bw_host'):
            env["BW_HOST"] = config['bw_host']
        if config.get('master_pw'):
            env["BW_PASSWORD"] = config['master_pw']
        env["MCP_SERVER_PATH"] = mcp_path

        proc = subprocess.Popen(
            ["node", mcp_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        init_request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "clientInfo": {"name": "Smart BW Setup", "version": "1.0.0"}}
        }) + "\n"

        stdin = proc.stdin
        if stdin:
            stdin.write(init_request)
            stdin.flush()

        import select
        stdout = proc.stdout
        ready = select.select([stdout], [], [], 5) if stdout else ([], [], [])
        if ready[0] and stdout:
            response = stdout.readline()
            if response and json.loads(response).get("result"):
                log_success("MCP 连接测试成功")
                proc.terminate()
                return True
        proc.terminate()
    except Exception as e:
        log_warning(f"MCP 连接测试跳过: {e}")

    status = check_bw_status(config.get('bw_host', ''))
    if status == "unlocked":
        log_success("bw 已解锁")
    elif status == "locked":
        log_warning("bw 已锁定（配置主密码后可自动解锁）")

    return True

# ============================================================================
# 核心安装函数（供 AI 和程序调用）
# ============================================================================

def setup_with_config(
    bw_host: Optional[str] = None,
    email: Optional[str] = None,
    master_pw: Optional[str] = None,
    api_key: Optional[str] = None,
    install_deps: bool = True,
    test_connect: bool = True,
    silent: bool = False
) -> dict:
    """
    程序化安装函数 - AI 助理调用此函数完成安装

    参数:
        bw_host: Vaultwarden 服务器地址 (https://...) - 必填
        email: 登录邮箱 - 可选
        master_pw: 主密码 - 必填（用于解密）
        api_key: API Key - 必填，推荐！（兼容性最强，支持所有 2FA）
        install_deps: 是否自动安装依赖
        test_connect: 是否测试连接
        silent: 是否静默模式

    返回:
        dict: {"success": bool, "message": str, "config": dict}

    使用示例:
        from setup import setup_with_config
        result = setup_with_config(
            bw_host="https://vaultwarden.example.com",
            email="user@example.com",
            master_pw="YourMasterPassword123",
            api_key="user.clientId.clientSecret"
        )
        print(result)
    """
    import builtins
    _print = builtins.print
    if silent:
        def noop(*args, **kwargs): pass
        print = noop

    try:
        # 参数验证
        if not bw_host:
            return {"success": False, "message": "bw_host 不能为空", "config": {}}
        if not email:
            return {"success": False, "message": "email 不能为空", "config": {}}
        if not master_pw:
            return {"success": False, "message": "master_pw 不能为空", "config": {}}

        # 构建配置
        config = {
            "bw_host": bw_host,
            "email": email,
            "master_pw": master_pw,
            "api_key": api_key,
        }

        # 检查/安装依赖
        if install_deps:
            if not check_python():
                return {"success": False, "message": "Python 版本过低", "config": {}}
            if not check_command("node"):
                return {"success": False, "message": "Node.js 未安装", "config": {}}
            if not check_bw_cli():
                install_bw_cli()
            mcp_path = check_mcp_server()
            if not mcp_path:
                install_mcp_server()
                mcp_path = check_mcp_server()
            if not mcp_path:
                return {"success": False, "message": "MCP Server 安装失败", "config": {}}
        else:
            mcp_path = check_mcp_server()
            if not mcp_path:
                return {"success": False, "message": "MCP Server 未安装", "config": {}}

        # 保存配置
        save_config(config, mcp_path)

        # 测试连接
        if test_connect:
            test_connection(config, mcp_path)

        print = _print
        return {
            "success": True,
            "message": "安装完成",
            "config": config,
            "mcp_path": mcp_path,
        }

    except Exception as e:
        print = _print
        return {"success": False, "message": str(e), "config": {}}

def ensure_installed(silent: bool = True) -> bool:
    """
    确保工具已安装并配置，返回是否可用
    """
    config = detect_existing_config()
    if all([config.get('bw_host'), config.get('email'), config.get('master_pw')]):
        return True

    result = setup_with_config(
        bw_host=config.get('bw_host') or '',
        email=config.get('email') or '',
        master_pw=config.get('master_pw') or '',
        api_key=config.get('api_key') or '',
        silent=silent
    )
    return result.get('success', False)

# ============================================================================
# 交互式主流程
# ============================================================================

def run_full_setup():
    """完整安装流程（交互式）"""
    print()
    print("=" * 60)
    print(f"{Colors.BLUE}Smart Bitwarden MCP 一键安装{Colors.NC}")
    print("=" * 60)

    sys_info = get_system_info()
    log_info(f"系统: {sys_info['os']} {sys_info['arch']}")
    log_info(f"Python: {sys_info['python_version']}")
    print()

    total_steps = 6
    step = 0

    # Step 1: 检查依赖
    step += 1
    log_step(step, total_steps, "检查基础依赖...")
    if not check_python() or not check_node() or not check_npm():
        return False

    # Step 2: 检查/安装 bw CLI
    step += 1
    log_step(step, total_steps, "检查 bw CLI...")
    if not check_bw_cli():
        log_info("正在安装...")
        if not install_bw_cli():
            log_warning("bw CLI 安装失败，继续...")

    # Step 3: 检查/安装 MCP Server
    step += 1
    log_step(step, total_steps, "检查 MCP Server...")
    mcp_path = check_mcp_server()
    if not mcp_path:
        if install_mcp_server():
            mcp_path = check_mcp_server()
    if not mcp_path:
        log_error("MCP Server 安装失败")
        return False

    # Step 4: 检测/获取配置
    step += 1
    log_step(step, total_steps, "检测配置...")
    config = detect_existing_config()

    has_all_secrets = all([config.get('bw_host'), config.get('email'), config.get('master_pw')])

    if has_all_secrets:
        log_success(f"服务器: {mask_sensitive(config.get('bw_host', ''))}")
        log_success(f"邮箱: {mask_sensitive(config.get('email', ''))}")
        log_success("主密码: 已配置")
        if config.get('api_key'):
            log_success("API Key: 已配置")

        print()
        reuse = input("使用现有配置? (y/n): ").strip().lower()
        if reuse != 'y':
            config = first_time_setup_wizard()
            if not config:
                return False
    else:
        log_warning("未检测到完整配置，需要您提供信息")
        config = first_time_setup_wizard()
        if not config:
            return False

    # Step 5: 保存配置
    step += 1
    log_step(step, total_steps, "保存配置...")
    save_config(config, mcp_path)

    # Step 6: 测试连接
    step += 1
    log_step(step, total_steps, "测试连接...")
    test_connection(config, mcp_path)

    print()
    print("=" * 60)
    log_success("安装完成！")
    print("=" * 60)
    print()
    print("使用方法:")
    print()
    print("  # Python")
    print("  import sys")
    print("  sys.path.insert(0, '/path/to/smart-bitwarden-mcp')")
    print("  from bw_for_weak_ai import get_pwd")
    print("  print(get_pwd('github'))")
    print()
    print("  # AI 助理调用")
    print("  from setup import setup_with_config")
    print("  setup_with_config(bw_host='...', email='...', master_pw='...')")
    print()
    print("=" * 60)

    return True

def run_check_only():
    """仅检查环境"""
    print()
    print("=" * 60)
    print(f"{Colors.BLUE}环境检查{Colors.NC}")
    print("=" * 60)
    print()

    check_python()
    check_node()
    check_npm()
    check_bw_cli()
    check_mcp_server()

    config = detect_existing_config()
    print()
    log_info("配置检测:")
    if config.get('bw_host'):
        log_success(f"  服务器: {mask_sensitive(config['bw_host'])}")
    if config.get('email'):
        log_success(f"  邮箱: {mask_sensitive(config['email'])}")
    if config.get('master_pw'):
        log_success("  主密码: 已配置")
    if config.get('api_key'):
        log_success("  API Key: 已配置")

    secrets_file = get_secrets_env_file()
    if secrets_file.exists():
        log_info(f"  敏感信息: {secrets_file}")

    status = check_bw_status(config.get('bw_host', ''))
    print()
    log_info(f"bw 状态: {status}")
    print()
    return True

def run_install_only():
    """仅安装依赖"""
    print()
    print("=" * 60)
    print(f"{Colors.BLUE}安装依赖{Colors.NC}")
    print("=" * 60)
    print()

    if not check_python() or not check_node():
        return False

    check_npm()

    if not check_bw_cli():
        install_bw_cli()

    mcp_path = check_mcp_server()
    if not mcp_path:
        install_mcp_server()
        mcp_path = check_mcp_server()

    if mcp_path:
        log_success("所有依赖安装完成")
        return True
    return False

def run_config_only():
    """仅配置"""
    print()
    print("=" * 60)
    print(f"{Colors.BLUE}配置{Colors.NC}")
    print("=" * 60)
    print()

    mcp_path = check_mcp_server()
    if not mcp_path:
        log_error("MCP Server 未安装，请先运行 --install")
        return False

    config = detect_existing_config()

    if not all([config.get('bw_host'), config.get('email'), config.get('master_pw')]):
        log_warning("未检测到完整配置，将引导您输入")
        config = first_time_setup_wizard()
        if not config:
            return False

    save_config(config, mcp_path)
    log_success("配置完成")
    return True

# ============================================================================
# 入口
# ============================================================================

def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]

        if arg in ("--check", "check"):
            run_check_only()
        elif arg in ("--install", "install"):
            run_install_only()
        elif arg in ("--config", "config"):
            run_config_only()
        elif arg in ("--help", "help", "-h"):
            print("Smart Bitwarden MCP 一键安装")
            print()
            print("用法:")
            print("  python3 setup.py              # 完整安装（交互式）")
            print("  python3 setup.py --check     # 仅检查环境")
            print("  python3 setup.py --install  # 仅安装依赖")
            print("  python3 setup.py --config   # 仅配置")
            print("  python3 setup.py --help     # 显示此帮助")
            print()
            print("AI/程序调用方式:")
            print("  from setup import setup_with_config")
            print("  setup_with_config(bw_host='...', email='...', master_pw='...')")
        else:
            log_error(f"未知参数: {arg}")
            print("使用 --help 查看帮助")
            sys.exit(1)
    else:
        run_full_setup()

if __name__ == "__main__":
    main()
