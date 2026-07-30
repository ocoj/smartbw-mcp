#!/usr/bin/env python3
"""
SmartBW MCP Daemon - 常驻 MCP 服务器守护进程

职责:
  1. 启动并管理一个长期运行的 node @bitwarden/mcp-server 进程
  2. 通过 Unix socket 接收 JSON-RPC 请求，代理到 mcp-server
  3. 自动解锁/重新登录
  4. 崩溃自动重启
  5. 支持多客户端并发

用法:
  python3 mcp_daemon.py                     # 前台运行
  python3 mcp_daemon.py --daemon            # 后台守护
  python3 mcp_daemon.py --stop              # 停止守护
  python3 mcp_daemon.py --status            # 查看状态

客户端连接:
  from mcp_raw import RealMCPClient
  client = RealMCPClient(use_daemon=True)   # 走 socket，不启动新进程
"""
import argparse
import atexit
import json
import logging
import os
import random
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

# 导入自定义异常（与 mcp_raw.py 使用的类型一致）
try:
    from models import ConnectionError as MCPConnectionError
    from models import TimeoutError as MCPTimeoutError
except ImportError:
    # 回退：用内置类型
    MCPTimeoutError = TimeoutError  # type: ignore
    MCPConnectionError = ConnectionError

# === 配置 ===
SOCKET_PATH = Path.home() / ".smartbw-mcp" / "daemon.sock"
PID_FILE = Path.home() / ".smartbw-mcp" / "daemon.pid"
LOG_FILE = Path.home() / ".smartbw-mcp" / "daemon.log"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [daemon] %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("mcp_daemon")

# 文件日志：每日轮转，保留 30 天（仅 daemon 启动时初始化，避免 import 副作用）
_file_handler = None


def _setup_file_logging():
    """初始化文件日志（每日轮转，保留 30 天）。仅 daemon 启动时调用。"""
    global _file_handler
    if _file_handler is not None:
        return  # 已初始化
    try:
        from logging.handlers import TimedRotatingFileHandler
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = TimedRotatingFileHandler(
            str(LOG_FILE), when='midnight', interval=1, backupCount=30,
            encoding='utf-8'
        )
        _file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [daemon] %(levelname)s %(message)s'
        ))
        _file_handler.setLevel(logging.INFO)
        logger.addHandler(_file_handler)
        logger.info("日志系统已启动 (保留 30 天)")
    except Exception:
        pass  # 文件日志非致命，失败时静默回退到 console

# === 工具函数 ===


def _load_config():
    """加载配置（复用 config.py）"""
    try:
        from config import get_config
        return get_config()
    except ImportError:
        return {}


def _ensure_logged_in_and_unlocked():
    """确保 bw 已登录且解锁，返回 BW_SESSION。系统启动时网络服务可能未就绪，重试最多 5 次。"""
    from unlock import auto_unlock
    max_retries = 5
    retry_delay = 10  # 秒
    for attempt in range(max_retries):
        session = auto_unlock()
        if session:
            logger.info(f"解锁成功, session 长度: {len(session)}")
            return session
        if attempt < max_retries - 1:
            logger.warning(f"第 {attempt+1}/{max_retries} 次解锁失败，{retry_delay}s 后重试...")
            time.sleep(retry_delay)
    logger.error(f"无法自动解锁（已重试 {max_retries} 次），守护进程无法启动")
    sys.exit(1)


def _find_mcp_server():
    """查找 MCP Server 路径"""
    cfg = _load_config()
    path = cfg.get("mcp_server_path", "")
    if path and Path(path).exists():
        return path

    candidates = [
        "/usr/local/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        "/usr/lib/node_modules/@bitwarden/mcp-server/dist/index.js",
        str(Path.home() / ".local/lib/node_modules/@bitwarden/mcp-server/dist/index.js"),
        str(Path.home() / ".npm-global/lib/node_modules/@bitwarden/mcp-server/dist/index.js"),
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


# === MCP Server 管理 ===


class MCPServerManager:
    """管理单个长期运行的 node mcp-server 子进程"""

    def __init__(self, bw_session: str):
        self.bw_session = bw_session
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._initialized = False

    def start(self, bw_session: Optional[str] = None) -> bool:
        """启动 MCP Server。可选传入新 session 替换旧 token。"""
        if bw_session:
            self.bw_session = bw_session

        mcp_path = _find_mcp_server()
        if not mcp_path:
            logger.error("找不到 MCP Server")
            return False

        env = os.environ.copy()
        env["BW_SESSION"] = self.bw_session
        bw_host = _load_config().get("bw_host", "")
        if not bw_host:
            logger.error("未配置 BW_HOST，请在 ~/.config/bitwarden-mcp/config.json 中设置 bw_host")
            return False
        env["BW_HOST"] = bw_host

        logger.info(f"启动 MCP Server: {mcp_path}")
        self.process = subprocess.Popen(
            ["node", mcp_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            bufsize=1,
            start_new_session=True,
        )
        time.sleep(0.5)
        if self.process.poll() is not None:
            logger.error("MCP Server 启动失败")
            return False

        # 初始化
        self._request_id = 0
        try:
            result = self._send_raw({
                "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "SmartBW Daemon", "version": "2.0.0"}
                }
            })
            if result:
                # MCP 规范要求：initialize 成功后发送 notifications/initialized
                try:
                    stdin = self.process.stdin
                    if stdin:
                        stdin.write(
                            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
                        )
                        stdin.flush()
                except Exception:
                    logger.warning("发送 notifications/initialized 失败，继续...")
                self._initialized = True
                logger.info("MCP Server 初始化成功（含 notifications/initialized 握手）")
                return True
        except Exception as e:
            logger.error(f"MCP 初始化失败: {e}")
        return False

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _send_raw(self, request: dict, timeout: float = 30.0) -> Optional[dict]:
        """直接发送 JSON-RPC 请求到子进程"""
        with self._lock:
            if not self.process or self.process.poll() is not None:
                raise ConnectionError("MCP 进程已退出")

            raw = json.dumps(request) + "\n"
            try:
                stdin = self.process.stdin
                if stdin:
                    stdin.write(raw)
                    stdin.flush()
            except Exception:
                raise ConnectionError("MCP stdin 写入失败")

            # 等待响应（跳过 notification，直到收到带 id 的响应）
            start = time.time()
            poll = select.poll()
            stdout = self.process.stdout
            poll.register(stdout, select.POLLIN) if stdout else None
            request_id = request.get("id")

            while (time.time() - start) < timeout:
                ready = poll.poll(100)
                if ready and stdout:
                    line = stdout.readline()
                    if not line:
                        if self.process.poll() is not None:
                            raise ConnectionError("MCP 进程退出")
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # 跳过 notification 和 id 不匹配的响应
                    if "method" in msg and "id" not in msg:
                        logger.debug(f"跳过 notification: {msg.get('method')}")
                        continue
                    if "id" in msg and msg["id"] == request_id:
                        return msg
                    logger.debug(f"跳过 id 不匹配: {msg.get('id')} != {request_id}")
                if self.process.poll() is not None:
                    raise ConnectionError("MCP 进程退出")

            raise MCPTimeoutError(f"MCP 响应超时 ({timeout}s)")

    def handle_request(self, request: dict) -> dict:
        """处理单个 JSON-RPC 请求"""
        method = request.get("method", "")
        # 内部命令：重启 MCP Server
        if method == "daemon/restart-mcp":
            logger.info("[EVENT] daemon_restart_mcp: 收到重启 MCP Server 请求")
            self.stop()
            if self.start():
                logger.info("[EVENT] daemon_restart_mcp: MCP Server 重启成功")
                return {"jsonrpc": "2.0", "id": request.get("id", 0), "result": {"status": "ok"}}
            else:
                logger.error("[EVENT] daemon_restart_mcp: MCP Server 重启失败")
                return {"jsonrpc": "2.0", "id": request.get("id", 0), "error": {"code": -32603, "message": "MCP server restart failed"}}
        # 不修改客户端 id，直接转发
        forwarded = {
            "jsonrpc": "2.0",
            "id": request.get("id", 0),
            "method": request["method"],
            "params": request.get("params", {}),
        }
        result = self._send_raw(forwarded)
        return result or {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": "No response"}}

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.process:
            logger.info("停止 MCP Server")
            try:
                # 杀整个进程组：确保所有 bw 子进程也被清理
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=3)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    self.process.kill()
                except OSError:
                    pass
            self.process = None


# === Unix Socket 服务器 ===


class DaemonServer:
    """Unix socket 服务器，接收客户端连接并代理请求"""

    def __init__(self, bw_session: str):
        self.bw_session = bw_session
        self.mcp = MCPServerManager(bw_session)
        self.server_sock: Optional[socket.socket] = None
        self._running = False
        self._clients = []

    def start(self):
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)

        # 清理旧 socket
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        # 安全：bind 前设 umask 确保 socket 文件创建时就是 0o600
        old_umask = os.umask(0o077)
        try:
            self.server_sock.bind(str(SOCKET_PATH))
        finally:
            os.umask(old_umask)

        self.server_sock.listen(5)
        self.server_sock.setblocking(False)
        # 防御纵深：bind 后再次确认权限
        os.chmod(str(SOCKET_PATH), 0o600)

        # 启动 MCP Server
        if not self.mcp.start():
            logger.error("MCP Server 启动失败")
            sys.exit(1)

        # 写 PID
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        logger.info(f"[EVENT] daemon_start: 守护进程已启动, socket: {SOCKET_PATH}, pid: {os.getpid()}")
        self._running = True

        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, lambda *_: self._shutdown())
        signal.signal(signal.SIGINT, lambda *_: self._shutdown())

        # 启动 MCP 子进程健康检查线程
        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_thread.start()

        self._run_loop()

    def _check_session_valid(self) -> bool:
        """检查当前 session 是否仍然有效（bw status 子进程检测）"""
        try:
            from unlock import _is_unlocked
            bw_host = _load_config().get("bw_host", "")
            if not bw_host:
                logger.error("未配置 bw_host，无法检查 session 有效性")
                return False
            env = os.environ.copy()
            env["BW_SESSION"] = self.bw_session
            env["BW_HOST"] = bw_host
            return _is_unlocked(env)
        except Exception as e:
            logger.warning(f"Session 有效性检查异常: {e}")
            return True  # 故障时保守，不误杀

    def _health_check_loop(self):
        """定期检查 MCP 进程健康 + Session 有效性，异常时自动恢复"""
        last_session_check = 0
        SESSION_CHECK_INTERVAL = 120  # 120 秒内检测并恢复过期 session

        while self._running:
            time.sleep(60)
            if not self._running:
                break

            now = time.time()
            # Session 有效性检查（每 120s 一次）
            if now - last_session_check > SESSION_CHECK_INTERVAL:
                last_session_check = now
                if self._check_session_valid():
                    logger.debug("[EVENT] session_check: valid")
                else:
                    logger.warning("[EVENT] session_expired: 检测到 Session 过期，开始自动恢复")
                    try:
                        from unlock import auto_unlock
                        new_session = auto_unlock()
                        if new_session:
                            logger.info("[EVENT] session_unlock: 解锁成功，重启 MCP 子进程")
                            self.bw_session = new_session
                            self.mcp.stop()
                            if self.mcp.start(bw_session=new_session):
                                logger.info("[EVENT] mcp_restart: MCP 子进程已用新 session 恢复")
                            else:
                                logger.error("[EVENT] mcp_restart_fail: MCP 子进程重启失败")
                        else:
                            logger.error("[EVENT] session_unlock_fail: 自动解锁失败，无法恢复 session")
                    except Exception as e:
                        logger.error(f"[EVENT] session_recover_error: {e}")

            # 进程健康检查
            if not self.mcp.is_alive():
                logger.warning("[EVENT] mcp_dead: MCP 子进程已死，尝试重启")
                try:
                    if self.mcp.start():
                        logger.info("[EVENT] mcp_recover: MCP 子进程已自动恢复")
                    else:
                        logger.error("[EVENT] mcp_recover_fail: MCP 子进程重启失败")
                except Exception as e:
                    logger.error(f"[EVENT] mcp_recover_error: {e}")

    def _run_loop(self):
        """事件循环：接受连接 + 读取客户端消息 + 代理到 MCP"""
        assert self.server_sock is not None, "server_sock must be initialized before _run_loop"
        poller = select.poll()
        poller.register(self.server_sock, select.POLLIN)

        client_buffers: Dict[int, bytes] = {}  # fd → buffer

        while self._running:
            try:
                events = poller.poll(500)  # 500ms
            except (OSError, select.error):
                continue

            for fd, event in events:
                if fd == self.server_sock.fileno():
                    # 新连接
                    try:
                        conn, _ = self.server_sock.accept()
                        conn.setblocking(False)
                        poller.register(conn, select.POLLIN)
                        client_buffers[conn.fileno()] = b""
                        self._clients.append(conn)
                        logger.debug(f"新客户端连接 fd={conn.fileno()}")
                    except OSError:
                        continue
                elif event & select.POLLIN:
                    # 客户端数据
                    conn = self._find_client_by_fd(fd)
                    if conn is None:
                        continue
                    try:
                        data = conn.recv(4096)
                        if not data:
                            self._close_client(conn, poller, client_buffers)
                            continue
                        client_buffers[conn.fileno()] += data
                        # 缓冲区上限保护，防止恶意/异常客户端导致 OOM
                        if len(client_buffers[conn.fileno()]) > 1_000_000:
                            logger.warning(f"客户端 fd={conn.fileno()} 缓冲区超限，断开")
                            self._close_client(conn, poller, client_buffers)
                            continue

                        # 尝试解析完整 JSON（以 \\n 分隔）
                        while b"\n" in client_buffers.get(conn.fileno(), b""):
                            line, _, rest = client_buffers[conn.fileno()].partition(b"\n")
                            client_buffers[conn.fileno()] = rest
                            try:
                                request = json.loads(line.decode())
                                response = self._handle(request)
                                conn.sendall((json.dumps(response) + "\n").encode())
                            except json.JSONDecodeError:
                                pass
                            except (BrokenPipeError, OSError):
                                self._close_client(conn, poller, client_buffers)
                                break
                    except (BrokenPipeError, OSError):
                        self._close_client(conn, poller, client_buffers)

    def _find_client_by_fd(self, fd):
        for conn in self._clients:
            if conn.fileno() == fd:
                return conn
        return None

    def _close_client(self, conn, poller, buffers):
        try:
            poller.unregister(conn)
        except (OSError, KeyError):
            pass
        buffers.pop(conn.fileno(), None)
        if conn in self._clients:
            self._clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass

    def _handle(self, request: dict) -> dict:
        """处理单个请求"""
        try:
            # 检查 MCP 进程健康
            if not self.mcp.is_alive():
                logger.warning("[EVENT] request_mcp_dead: MCP 进程已死, 尝试重启")
                if not self.mcp.start():
                    return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": "MCP server restart failed"}}

            return self.mcp.handle_request(request)
        except Exception as e:
            logger.error(f"[EVENT] request_error: method={request.get('method', '?')} error={e}")
            return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": str(e)}}

    def _shutdown(self):
        logger.info("[EVENT] daemon_shutdown: 收到关闭信号")
        self._running = False
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        # 等待健康检查线程退出（最多 3s）
        if hasattr(self, '_health_thread') and self._health_thread.is_alive():
            self._health_thread.join(timeout=3)
        self.mcp.stop()
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        if PID_FILE.exists():
            PID_FILE.unlink()
        logger.info("守护进程已停止")


# === 客户端接口 ===


class DaemonClient:
    """连接到守护进程的轻量客户端（供 RealMCPClient 使用）

    线程安全：send_request 通过 _send_lock 保护收发原子性。
    """

    def __init__(self, timeout: int = 30):
        self.sock: Optional[socket.socket] = None
        self.timeout = timeout
        self._send_lock = threading.Lock()  # 保护 send/recv 原子性

    def connect(self) -> bool:
        if not SOCKET_PATH.exists():
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(str(SOCKET_PATH))
            self.sock = sock
            return True
        except (OSError, ConnectionRefusedError):
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None
            return False

    def is_connected(self) -> bool:
        return self.sock is not None

    def send_request(self, method: str, params: dict) -> dict:
        """发送请求（线程安全：锁保护收发原子性 + 逐 chunk 超时）"""
        if not self.sock:
            raise ConnectionError("未连接到守护进程")

        request = {
            "jsonrpc": "2.0",
            "id": random.randint(1, 999999),
            "method": method,
            "params": params,
        }
        req_id = request["id"]
        raw = json.dumps(request) + "\n"

        with self._send_lock:
            self.sock.sendall(raw.encode())
            buf = b""
            start = time.time()
            while (time.time() - start) < self.timeout:
                # 每次 recv 独立超时（min 5s, 剩余时间）
                remaining = max(0.5, self.timeout - (time.time() - start))
                try:
                    self.sock.settimeout(min(5.0, remaining))
                    chunk = self.sock.recv(4096)
                except (socket.timeout, BlockingIOError):
                    continue
                if not chunk:
                    raise ConnectionError("守护进程断开")
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    try:
                        msg = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    if "method" in msg and "id" not in msg:
                        continue
                    if msg.get("id") == req_id:
                        return msg
            raise MCPTimeoutError(f"守护进程响应超时 ({self.timeout}s)")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# === CLI ===


def _is_daemon_running():
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return SOCKET_PATH.exists()
    except (OSError, ValueError):
        return False


def _stop_daemon():
    if not _is_daemon_running():
        print("守护进程未运行")
        return
    pid = int(PID_FILE.read_text().strip())
    os.kill(pid, signal.SIGTERM)
    print(f"已发送停止信号到 PID {pid}")


def _start_daemon(foreground=False):
    if _is_daemon_running():
        print("守护进程已在运行")
        sys.exit(1)

    if not foreground:
        # 后台运行
        pid = os.fork()
        if pid > 0:
            print(f"守护进程已启动 (PID {pid})")
            sys.exit(0)
        os.setsid()

    _setup_file_logging()
    session = _ensure_logged_in_and_unlocked()
    server = DaemonServer(session)
    server.start()


def main():
    parser = argparse.ArgumentParser(description="SmartBW MCP Daemon")
    parser.add_argument("--daemon", action="store_true", help="后台守护模式")
    parser.add_argument("--stop", action="store_true", help="停止守护")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--restart", action="store_true", help="重启守护")
    parser.add_argument("--reinit", action="store_true", help="重新初始化: 清空加密凭证, 备份 config.json")

    args = parser.parse_args()

    if args.reinit:
        from crypto_config import reinit_config
        reinit_config()
        return

    if args.stop:
        _stop_daemon()
    elif args.status:
        if _is_daemon_running():
            pid = PID_FILE.read_text().strip()
            print(f"✅ 运行中 (PID {pid})")
        else:
            print("❌ 未运行")
    elif args.restart:
        _stop_daemon()
        time.sleep(1)
        _start_daemon(foreground=False)
    elif args.daemon:
        _start_daemon(foreground=False)
    else:
        # 默认前台运行
        _start_daemon(foreground=True)


if __name__ == "__main__":
    main()
