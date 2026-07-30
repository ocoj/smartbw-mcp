"""
MCP 客户端模块 - 通过守护进程连接 Vaultwarden

负责:
- 连接 smartbw-daemon (Unix Socket)
- JSON-RPC 通信
- Bitwarden 操作(list / get / create / update / delete)
- 熔断保护
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import CONFIG, DEFAULT_TIMEOUT, logger
from models import ConnectionError, LockedError, TimeoutError

# ============================================================================
# MCP 客户端（纯 daemon 模式）
# ============================================================================

class RealMCPClient:
    """MCP 客户端 — 通过 Unix Socket 连接 smartbw-daemon"""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, use_daemon: bool = True):
        self.initialized = False
        self.timeout = timeout
        self._daemon_client: 'DaemonClient' = None  # type: ignore

        # 熔断器
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._circuit_open_until = 0.0
        self._max_failures = 5
        self._circuit_cooldown = 30.0

        self.bw_host = CONFIG.get("bw_host", "https://your-vaultwarden-server.com")
        logger.info(f"Bitwarden 服务器: {self.bw_host}")

        try:
            from mcp_daemon import DaemonClient
            self._daemon_client = DaemonClient(timeout=self.timeout)
        except ImportError as e:
            raise ConnectionError(f"无法导入 mcp_daemon 模块: {e}")

    # ─── JSON-RPC 通信 ──────────────────────

    def _send_request(self, method: str, params: Dict) -> Dict:
        """发送 JSON-RPC 请求（统一走 daemon socket）"""
        logger.debug(f"发送: {method}")
        try:
            result = self._daemon_client.send_request(method, params)
            if "error" in result:
                error = result["error"]
                raise Exception(f"MCP 错误 [{error.get('code')}]: {error.get('message')}")
            return result.get("result", {})
        except TimeoutError:
            raise
        except Exception as e:
            # 使用类型检查替代字符串匹配，避免 locale 差异
            if isinstance(e, (OSError, socket.error)):
                logger.warning("守护进程断开，尝试重连...")
            elif "Connection" in str(e) or "BrokenPipe" in str(e):
                # 兜底：catch-all 字符串匹配（处理来自模型的包装异常）
                logger.warning("守护进程断开（字符串匹配），尝试重连...")
            else:
                # 非连接类异常直接抛出
                raise

            try:
                client = self._daemon_client
                client.close()
                client.connect()
                result = client.send_request(method, params)
                if "error" in result:
                    error = result["error"]
                    raise Exception(f"MCP 错误 [{error.get('code')}]: {error.get('message')}")
                return result.get("result", {})
            except Exception as retry_err:
                raise ConnectionError(f"守护进程重连失败: {retry_err}")

    # ─── 连接管理 ──────────────────────────

    def start(self) -> bool:
        """连接守护进程"""
        if self._daemon_client.is_connected():
            return True
        if self._daemon_client.connect():
            logger.info("已连接守护进程")
            return True

        logger.warning("守护进程未运行，尝试启动...")
        try:
            subprocess.Popen(
                [sys.executable, os.path.join(os.path.dirname(__file__), "mcp_daemon.py"), "--daemon"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for retry in range(15):
                time.sleep(0.5)
                if self._daemon_client.connect():
                    logger.info(f"守护进程已自动启动并连接（{retry + 1}次重试）")
                    return True
            logger.error("守护进程启动后连接超时")
        except Exception as e:
            logger.error(f"自动启动守护进程失败: {e}")
        return False

    def ping(self) -> bool:
        """健康检查"""
        return self._daemon_client.is_connected()

    def initialize(self) -> bool:
        """初始化连接"""
        if self.initialized and self._daemon_client.is_connected():
            return True
        if self.start():
            self.initialized = True
            return True
        return False

    def close(self):
        """关闭 socket 连接（不杀守护进程）"""
        if self._daemon_client:
            self._daemon_client.close()
        self.initialized = False

    # ─── 工具调用 ──────────────────────────

    def call_tool(self, tool_name: str, arguments: Optional[Dict] = None) -> Any:
        """调用 MCP 工具"""
        if not self.initialized:
            if not self.initialize():
                raise Exception("MCP 初始化失败")

        arguments = arguments or {}

        # Vaultwarden 兼容: generate 的 boolean=False 会触发不支持的标志
        if tool_name == "generate":
            arguments = {k: v for k, v in arguments.items() if v is not False}

        logger.debug(f"调用工具: {tool_name}")

        try:
            result = self._send_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments}
            )
        except TimeoutError:
            self.initialized = False
            raise

        content = result.get("content", [])
        if not content:
            return None

        first_content = content[0]
        text = first_content.get("text", "")
        if not text:
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ─── 熔断器 ────────────────────────────

    def _is_circuit_open(self) -> bool:
        return self._circuit_open_until > time.time()

    def _reset_circuit(self):
        self._failure_count = 0
        self._circuit_open_until = 0.0

    def _record_failure(self):
        now = time.time()
        if now - self._last_failure_time > 120:
            self._failure_count = 0
        self._failure_count += 1
        self._last_failure_time = now
        if self._failure_count >= self._max_failures:
            self._circuit_open_until = now + self._circuit_cooldown
            logger.warning(f"熔断器打开，{self._circuit_cooldown}s 后重试（{self._failure_count} 次连续失败）")

    def _with_circuit(self, operation_func, *args, **kwargs):
        """带熔断保护的操作执行"""
        if self._is_circuit_open():
            remaining = int(self._circuit_open_until - time.time())
            raise LockedError(f"熔断器打开，{remaining}s 后重试")

        try:
            result = operation_func(*args, **kwargs)
            self._reset_circuit()
            return result
        except (LockedError, TimeoutError, ConnectionError):
            self._record_failure()
            raise

    # ─── Bitwarden 操作 ─────────────────────

    def list_items(self, type: str = "items", **kwargs) -> List[Dict]:
        """列出项目"""
        def _do():
            args = {"type": type, "trash": False}
            args.update(kwargs)
            result = self.call_tool("list", args)
            if isinstance(result, list):
                return result
            if isinstance(result, str):
                try:
                    return json.loads(result)
                except Exception:
                    return []
            return []

        try:
            return self._with_circuit(_do)
        except LockedError:
            logger.error("list_items 失败（熔断/锁定）")
            return []
        except TimeoutError:
            logger.error("list_items 超时")
            return []
        except ConnectionError:
            logger.error("list_items 连接失败")
            return []

    def get_item(self, item_id: str) -> Optional[Dict]:
        """获取项目详情"""
        def _do():
            result = self.call_tool("get", {"object": "item", "id": item_id})
            return result if isinstance(result, dict) else None

        try:
            return self._with_circuit(_do)
        except (LockedError, TimeoutError):
            return None
        except ConnectionError:
            logger.error("get_item 连接失败")
            return None

    def get_password(self, item_id: str) -> Optional[str]:
        """获取密码"""
        def _do():
            result = self.call_tool("get", {"object": "password", "id": item_id})
            return result if isinstance(result, str) else None

        try:
            return self._with_circuit(_do)
        except (LockedError, TimeoutError):
            return None
        except ConnectionError:
            logger.error("get_password 连接失败")
            return None

    def create_item(self, name: str, username: str = "", password: str = "",
                    uri: str = "", notes: str = "") -> Optional[str]:
        """创建项目，返回 ID"""
        def _do():
            login = {}
            if username:
                login["username"] = username
            if password:
                login["password"] = password
            if uri:
                login["uris"] = [{"uri": uri}]
            item_data = {"name": name, "type": 1, "notes": notes}
            if login:
                item_data["login"] = login
            result = self.call_tool("create_item", item_data)
            if isinstance(result, dict):
                return result.get("id")
            if isinstance(result, str) and result:
                try:
                    return json.loads(result).get("id")
                except Exception:
                    pass
            return None

        try:
            return self._with_circuit(_do)
        except (LockedError, TimeoutError):
            logger.error("create_item 失败")
            return None
        except Exception as e:
            logger.error(f"create_item 失败: {e}")
            return None

    def update_item(self, item_id: str, name: Optional[str] = None, username: Optional[str] = None,
                    password: Optional[str] = None, uri: Optional[str] = None, notes: Optional[str] = None) -> bool:
        """更新项目"""
        def _do():
            params = {"id": item_id}
            if name is not None:
                params["name"] = name
            if notes is not None:
                params["notes"] = notes
            login_updates = {}
            if username is not None:
                login_updates["username"] = username
            if password is not None:
                login_updates["password"] = password
            if uri is not None:
                login_updates["uris"] = [{"uri": uri}]
            if login_updates:
                params["login"] = login_updates  # type: ignore[assignment]
            return self.call_tool("edit_item", params) is not None

        try:
            return self._with_circuit(_do)
        except (LockedError, TimeoutError):
            return False
        except Exception as e:
            logger.error(f"update_item 失败: {e}")
            return False

    def delete_item(self, item_id: str) -> bool:
        """删除项目"""
        def _do():
            return self.call_tool("delete", {"object": "item", "id": item_id}) is not None

        try:
            return self._with_circuit(_do)
        except (LockedError, TimeoutError):
            return False
        except Exception as e:
            logger.error(f"delete_item 失败: {e}")
            return False
