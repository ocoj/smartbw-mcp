"""
Config 加密/解密模块 — 保护 config.json 中敏感字段

密钥派生: HKDF-SHA256(hostname + machine-id, salt="smartbw-config-v1")
加密: Fernet (AES-128-CBC + HMAC-SHA256, 认证加密)

格式: "!enc:v1:{base64url(Fernet token)}"

设计目标: config.json 意外泄露时 master_password / client_secret / api_key 不可读
不可防: 本机 root 攻击（可读 machine-id 和 hostname）

路径解析统一走 `paths`，与 config.py 保持同一语义（支持 SMARTBW_CONFIG_DIR）。
"""
import base64
import json
import logging
import os
import shutil
import socket
from pathlib import Path
from typing import Dict, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from paths import config_path, env_path, is_default_runtime_dir

logger = logging.getLogger(__name__)

# 需要加密落盘的敏感字段。
# api_key 是 "user.<clientId>.<clientSecret>" 单字段形式，内含 clientSecret，必须一并保护。
SENSITIVE_KEYS = ["master_password", "client_secret", "api_key"]
ENCRYPTED_PREFIX = "!enc:v1:"
REINIT_FILE = Path.home() / ".smartbw-mcp" / "NEEDS_REINIT"


def __getattr__(name):
    """兼容旧引用：CONFIG_PATH / ENV_PATH 改为每次访问时重新解析。

    不能在 import 期求值 —— 否则 SMARTBW_CONFIG_DIR 一旦在进程内变更
    （含 get_config(refresh=True) 场景），路径会停留在首次 import 时的值。
    """
    if name == "CONFIG_PATH":
        return config_path()
    if name == "ENV_PATH":
        return env_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_machine_id() -> str:
    """获取稳定的机器指纹"""
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            with open(path) as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError):
            pass
    return socket.gethostname()


def _derive_key() -> bytes:
    """HKDF-SHA256 从机器指纹派生 32 字节 Fernet key"""
    hostname = socket.gethostname()
    machine_id = _get_machine_id()
    ikm = f"{hostname}:{machine_id}:smartbw-config-v2".encode()

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"smartbw-config-salt",
        info=b"smartbw-fernet-key",
    )
    key_material = hkdf.derive(ikm)
    return base64.urlsafe_b64encode(key_material)


def encrypt_value(plaintext: str) -> str:
    """加密单个值，返回 "!enc:v1:..." 格式"""
    if not plaintext:
        return plaintext
    f = Fernet(_derive_key())
    return ENCRYPTED_PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_value(encrypted: str) -> Optional[str]:
    """解密 "!enc:v1:..." 格式的值，失败返回 None"""
    if not encrypted or not encrypted.startswith(ENCRYPTED_PREFIX):
        return None
    try:
        f = Fernet(_derive_key())
        return f.decrypt(encrypted[len(ENCRYPTED_PREFIX):].encode()).decode()
    except Exception as e:
        logger.warning(f"配置解密失败 (机器指纹变更?): {e}")
        return None


def _load_env_master_password() -> Optional[str]:
    """回退: 从 .secrets/.env 读取主密码"""
    try:
        path = env_path()
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("BW_MASTER_PASSWORD="):
                        val = line.split("=", 1)[1].strip().strip("'").strip('"')
                        if val:
                            return val
    except (OSError, PermissionError):
        pass
    return None


def _write_reinit(message: str):
    """写入 NEEDS_REINIT 标记文件。

    位置固定在运行目录 `~/.smartbw-mcp/`（不属于配置目录，故不受 SMARTBW_CONFIG_DIR 影响）。
    """
    try:
        REINIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REINIT_FILE.write_text(message)
    except (OSError, PermissionError):
        pass
    logger.error(message)


def _marker_belongs_to(cfg_file: Path) -> bool:
    """判断现有标记是否描述"本配置目录"。

    标记位置是全局的（运行目录），因此不能无条件删除 —— 否则用 SMARTBW_CONFIG_DIR
    指向别的目录时，会把默认目录的合法标记误删（跨目录干扰，与 P0-2 同族）。
    我们写入的内容里含 config.json 的绝对路径，可据此归属判断。
    """
    if is_default_runtime_dir():
        return True  # 默认目录：沿用原行为，历史标记一律清理
    try:
        return str(cfg_file) in REINIT_FILE.read_text()
    except (OSError, PermissionError):
        return False


def process_config_on_startup() -> Dict[str, str]:
    """
    守护进程启动时调用:
    1. 已加密字段 → 解密到内存
    2. 明文字段 → 自动加密写回 config.json
    3. 解密失败 → .secrets/.env 兜底 → 自动重新加密
    4. 都失败 → 写 NEEDS_REINIT, 返回空 dict, 守护进程退出

    返回: 解密后的 config dict (master_password 等已还原为明文)
    """
    cfg_file = config_path()
    if not cfg_file.exists():
        return {}

    try:
        with open(cfg_file) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"读取 config.json 失败: {e}")
        return {}

    need_save = False
    result = dict(config)
    plaintext_found = False

    for key in SENSITIVE_KEYS:
        val = config.get(key, "")
        if not val:
            result[key] = ""
            continue

        if val.startswith(ENCRYPTED_PREFIX):
            plain = decrypt_value(val)
            if plain is not None:
                result[key] = plain
            else:
                if key == "master_password":
                    fallback = _load_env_master_password()
                    if fallback:
                        logger.info("config.json 解密失败, 从 .secrets/.env 回退成功")
                        result[key] = fallback
                        config[key] = fallback
                        plaintext_found = True
                        need_save = True
                    else:
                        _write_reinit(
                            f"加密凭证解密失败且 master_password 无回退源\n"
                            f"原因: 机器指纹已变更 (hostname={socket.gethostname()})\n"
                            f"修复: 编辑 {cfg_file}\n"
                            f"      将 master_password 设为新的明文密码\n"
                            f"      重启: systemctl --user restart smartbw-daemon"
                        )
                        logger.error("解密 master_password 失败且无回退源")
                        return {}
                else:
                    result[key] = ""
                    logger.warning(f"解密 {key} 失败, 清空 (非必须字段)")
        else:
            # 明文 → 标记需加密
            plaintext_found = True

    if plaintext_found:
        for key in SENSITIVE_KEYS:
            if config.get(key) and not str(config[key]).startswith(ENCRYPTED_PREFIX):
                config[key] = encrypt_value(str(config[key]))
                need_save = True

    if need_save:
        try:
            config_path_str = str(cfg_file)
            tmp = config_path_str + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)  # 写完后立即设权限
            os.replace(tmp, config_path_str)
            logger.info("config.json 已加密保存 (敏感字段已替换为 !enc:...)")
        except (OSError, PermissionError) as e:
            logger.error(f"写入 config.json 失败: {e}")

    # 清理 reinit 标记（仅当它属于本配置目录，避免跨目录干扰）
    if REINIT_FILE.exists() and _marker_belongs_to(cfg_file):
        REINIT_FILE.unlink()

    return result


def reinit_config():
    """命令行 --reinit: 备份 config.json, 清空敏感字段"""
    cfg_file = config_path()
    if not cfg_file.exists():
        print(f"config.json 不存在: {cfg_file}")
        return

    bak = cfg_file.with_suffix(".reinit.bak")
    shutil.copy2(cfg_file, bak)
    print(f"已备份: {bak}")

    with open(cfg_file) as f:
        config = json.load(f)
    for key in SENSITIVE_KEYS:
        config[key] = ""
    with open(cfg_file, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    _write_reinit(
        f"需要重新配置凭证\n"
        f"操作: 编辑 {cfg_file}\n"
        f"      将 master_password 设为新的明文密码\n"
        f"      重启: systemctl --user restart smartbw-daemon"
    )
    print("敏感字段已清空, 请编辑 config.json 填入新凭证后重启守护进程")


if __name__ == "__main__":
    import sys
    if "--reinit" in sys.argv:
        reinit_config()
    else:
        result = process_config_on_startup()
        pw = result.get("master_password", "")
        print(f"master_password={'***' if pw else '(empty)'}")
        print(f"client_secret={'***' if result.get('client_secret') else '(empty)'}")
