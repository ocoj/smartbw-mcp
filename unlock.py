"""
自动解锁模块 - 从 mcp_raw.py 提取的自动解锁/登录逻辑

负责:
- bw unlock 自动解锁
- bw login 自动登录（API Key / 用户名密码）
- Session token 解析和获取
- bw CLI 进程管理
"""
import os
import re
import json
import subprocess
import logging
from typing import Optional

from config import CONFIG, AUTO_UNLOCK, CLI_STATUS_TIMEOUT, CLI_LOGIN_TIMEOUT, CLI_UNLOCK_TIMEOUT, logger

# ============================================================================
# 自动解锁
# ============================================================================


def auto_unlock() -> Optional[str]:
    """
    自动解锁金库
    从环境变量或配置文件获取主密码,调用 bw unlock 获取新 session
    返回新 session token,失败返回 None
    """
    if not AUTO_UNLOCK:
        return None

    # 获取主密码
    master_password = os.environ.get("BW_MASTER_PASSWORD")
    if not master_password:
        master_password = CONFIG.get("master_password")
    if not master_password:
        logger.warning("未配置 BW_MASTER_PASSWORD,无法自动解锁")
        return None

    try:
        logger.info("尝试自动解锁...")

        bw_host = CONFIG.get("bw_host", "https://your-vaultwarden-server.com")
        unlock_env = _prepare_env(bw_host, master_password)

        if not _ensure_logged_in(unlock_env):
            return None

        return _do_unlock(unlock_env)

    except FileNotFoundError:
        logger.error("bw CLI 未找到,请安装 Bitwarden CLI")
        return None
    except Exception as e:
        logger.error(f"自动解锁异常: {e}")
        return None


def _prepare_env(bw_host: str, master_password: str) -> dict:
    """准备子进程环境变量"""
    env = os.environ.copy()
    env["BW_HOST"] = bw_host
    env["BW_PASSWORD"] = master_password
    return env


def _ensure_logged_in(env: dict) -> bool:
    """确保已登录，未登录则尝试自动登录"""
    if _is_unlocked(env):
        return True

    logger.info("bw 未解锁或未登录，尝试自动登录...")

    # 优先使用 API Key
    if _try_api_key_login(env):
        return True

    # 回退用户名密码
    return _try_password_login(env)


def _is_unlocked(env: dict) -> bool:
    """检查 bw 状态是否已解锁（解析 JSON 输出）"""
    try:
        check_proc = subprocess.run(
            ["bw", "status"],
            env=env, capture_output=True, text=True, timeout=CLI_STATUS_TIMEOUT
        )
        # 优先尝试 JSON 解析（bw status 输出格式）
        try:
            status = json.loads(check_proc.stdout.strip())
            # v2.0.1: 修复 v2.0.0 引入的 return 值反置 bug
            # "unlocked" → True (一切正常)
            # "locked" → True (已登录, 只差 unlock — _do_unlock 会处理)
            # "unauthenticated" → False (未登录, 需要 _try_api_key_login/_try_password_login)
            st = status.get("status", "")
            return st in ("unlocked", "locked")
        except (json.JSONDecodeError, ValueError):
            pass
        # 回退字符串匹配（兼容非标准输出）
        output = check_proc.stdout.lower()
        if "not logged in" in output or '"loggedin": false' in output:
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.debug(f"bw status 检查失败: {e}")
        return False


def _try_api_key_login(env: dict) -> bool:
    """尝试使用 API Key 登录"""
    client_id, client_secret = _get_api_credentials()
    if not client_id or not client_secret:
        return False

    login_env = env.copy()
    login_env["BW_CLIENTID"] = client_id
    login_env["BW_CLIENTSECRET"] = client_secret
    logger.info(f"使用 API Key 登录 (client_id: {client_id[:12]}...)")

    try:
        login_proc = subprocess.run(
            ["bw", "login", "--apikey"],
            env=login_env, capture_output=True, text=True, timeout=CLI_LOGIN_TIMEOUT
        )
        if login_proc.returncode == 0:
            logger.info("API Key 登录成功")
            return True
        logger.warning(f"API Key 登录失败 (exit: {login_proc.returncode})")
        return False
    except (subprocess.TimeoutExpired, Exception) as e:
        logger.debug(f"API Key 登录异常: {e}")
        return False


def _try_password_login(env: dict) -> bool:
    """尝试使用用户名密码登录"""
    email = os.environ.get("BW_EMAIL") or CONFIG.get("email", "")
    if not email:
        logger.warning("未配置 BW_EMAIL，无法使用用户名密码登录")
        return False

    master_password = os.environ.get("BW_MASTER_PASSWORD") or CONFIG.get("master_password", "")
    if not master_password:
        logger.warning("未配置 BW_MASTER_PASSWORD，无法登录")
        return False

    logger.info(f"尝试用户名密码登录 {email[:12]}...")
    try:
        # v2.0.1: 传入 env (含 BW_HOST) + 使用 --passwordenv 传递密码
        # 修复两个 bug: 1) env 漏传导致 BW_HOST 不生效 2) stdin 密码不可靠
        login_proc = subprocess.run(
            ["bw", "login", email, "--passwordenv", "BW_PASSWORD"],
            env=env,
            capture_output=True, text=True, timeout=CLI_LOGIN_TIMEOUT
        )
        if login_proc.returncode == 0:
            logger.info("用户名密码登录成功")
            return True
        logger.error(f"用户名密码登录失败 (exit: {login_proc.returncode})")
        return False
    except (subprocess.TimeoutExpired, Exception) as e:
        logger.debug(f"用户名密码登录异常: {e}")
        return False


def _get_api_credentials() -> tuple:
    """获取 API Key 凭证 (client_id, client_secret)"""
    client_id = os.environ.get("BW_CLIENTID") or CONFIG.get("client_id", "")
    client_secret = os.environ.get("BW_CLIENTSECRET") or CONFIG.get("client_secret", "")

    # 尝试从 BW_API_KEY 解析
    if not client_id or not client_secret:
        api_key = os.environ.get("BW_API_KEY") or CONFIG.get("api_key", "")
        if api_key:
            if api_key.startswith("user."):
                parts = api_key.split(".")
                if len(parts) >= 3:
                    client_id = parts[0] + "." + parts[1]
                    client_secret = ".".join(parts[2:])
                else:
                    logger.warning(f"API Key 格式无效（段数={len(parts)}），跳过 API Key 登录")
            else:
                client_id = api_key
                client_secret = client_secret or ""

    return client_id, client_secret


def _do_unlock(env: dict) -> Optional[str]:
    """执行 bw unlock 并解析 token"""
    try:
        proc = subprocess.run(
            ["bw", "unlock", "--passwordenv", "BW_PASSWORD"],
            env=env, capture_output=True, text=True, timeout=CLI_UNLOCK_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        logger.error("bw unlock 超时")
        return None

    if proc.returncode != 0:
        logger.error(f"bw unlock 失败 (exit: {proc.returncode})")
        return None

    token = _extract_session_token(proc.stdout)
    if token:
        logger.info(f"自动解锁成功, token 长度: {len(token)}")
    else:
        logger.warning(f"自动解锁成功但未解析到 token, 输出: {proc.stdout[:200]}")
    return token


def _extract_session_token(output: str) -> Optional[str]:
    """从 bw unlock 输出中提取 session token"""
    patterns = [
        r'BW_SESSION[=:]?\s*["\']?([a-zA-Z0-9+/=]{50,})["\']?',
        r'export\s+BW_SESSION=["\']([^"\']+)["\']',
        r'\$env:BW_SESSION=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            token = match.group(1).strip()
            if len(token) > 50:
                return token

    # 回退：最后一行长字符串
    for line in reversed(output.strip().split('\n')):
        line = line.strip()
        if line and not line.startswith(('Your vault', 'To unlock', '$')):
            if len(line) > 50 and ('=' in line or line.startswith('uB') or line.startswith('J')):
                return line

    return None
