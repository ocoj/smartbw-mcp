"""
Config 加密/解密模块 — 保护 config.json 中敏感字段

密钥派生: HKDF-SHA256(hostname + machine-id, salt="smartbw-config-v1")
加密: Fernet (AES-128-CBC + HMAC-SHA256, 认证加密)

格式: "!enc:v1:{base64url(Fernet token)}"

设计目标: config.json 意外泄露时 master_password/client_secret 不可读
不可防: 本机 root 攻击（可读 machine-id 和 hostname）
"""
import os
import json
import socket
import base64
import logging
import shutil
from pathlib import Path
from typing import Optional, Dict

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = ["master_password", "client_secret"]
ENCRYPTED_PREFIX = "!enc:v1:"
CONFIG_PATH = Path.home() / ".config" / "bitwarden-mcp" / "config.json"
ENV_PATH = CONFIG_PATH.parent / ".env"  # 与 config.json 同目录的 .env 兜底
REINIT_FILE = Path.home() / ".smartbw-mcp" / "NEEDS_REINIT"


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
        if ENV_PATH.exists():
            with open(ENV_PATH) as f:
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
    """写入 NEEDS_REINIT 标记文件"""
    try:
        REINIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REINIT_FILE.write_text(message)
    except (OSError, PermissionError):
        pass
    logger.error(message)


def process_config_on_startup() -> Dict[str, str]:
    """
    守护进程启动时调用:
    1. 已加密字段 → 解密到内存
    2. 明文字段 → 自动加密写回 config.json
    3. 解密失败 → .secrets/.env 兜底 → 自动重新加密
    4. 都失败 → 写 NEEDS_REINIT, 返回空 dict, 守护进程退出

    返回: 解密后的 config dict (master_password 等已还原为明文)
    """
    if not CONFIG_PATH.exists():
        return {}

    try:
        with open(CONFIG_PATH) as f:
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
                            f"修复: 编辑 {CONFIG_PATH}\n"
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
            config_path_str = str(CONFIG_PATH)
            tmp = config_path_str + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)  # 写完后立即设权限
            os.replace(tmp, config_path_str)
            logger.info("config.json 已加密保存 (敏感字段已替换为 !enc:...)")
        except (OSError, PermissionError) as e:
            logger.error(f"写入 config.json 失败: {e}")

    # 清理旧的 reinit 标记
    if REINIT_FILE.exists():
        REINIT_FILE.unlink()

    return result


def reinit_config():
    """命令行 --reinit: 备份 config.json, 清空敏感字段"""
    if not CONFIG_PATH.exists():
        print(f"config.json 不存在: {CONFIG_PATH}")
        return

    bak = CONFIG_PATH.with_suffix(".reinit.bak")
    shutil.copy2(CONFIG_PATH, bak)
    print(f"已备份: {bak}")

    with open(CONFIG_PATH) as f:
        config = json.load(f)
    for key in SENSITIVE_KEYS:
        config[key] = ""
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    _write_reinit(
        f"需要重新配置凭证\n"
        f"操作: 编辑 {CONFIG_PATH}\n"
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
