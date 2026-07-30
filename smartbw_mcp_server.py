#!/usr/bin/env python3
"""
SmartBW MCP Server - 注册为 OpenClaw MCP Server

通过 stdio 协议提供 smartbw 专用工具:
  - smartbw_get_api(name)       → 一行获取 API Key
  - smartbw_get_field(name, f)  → 获取自定义字段
  - smartbw_search(q)           → 模糊搜索（多结果列出选项）
  - smartbw_get_item(name)      → 获取完整信息
  - smartbw_daemon_status()     → 检查守护进程状态

用法（MCP 客户端配置）:
  "smartbw": {
    "type": "stdio",
    "command": "python3",
    "args": ["/path/to/smartbw_mcp_server.py"]
  }
"""
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format='%(levelname)s [smartbw-mcp] %(message)s')
logger = logging.getLogger("smartbw_mcp_server")

# 优雅退出信号文件 — 部署脚本 touch 此文件后，MCP server 处理完下一个请求即退出
SHUTDOWN_SIGNAL = Path.home() / ".smartbw-mcp" / "restart.signal"

# 提前导入（避免每次请求重复 import）
from config import get_config
from smart_search import SmartBitwardenMCP

# === 工具定义 ===

TOOLS = [
    {
        "name": "smartbw_get_api",
        "description": "从 Vaultwarden 获取 API Key。自动搜索名为 'API' 的自定义字段，无需手动解析 JSON。支持模糊搜索项目名。多结果时可指定 index。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "项目名称搜索词，如 'deepseek'、'github'"},
                "index": {"type": "integer", "description": "多结果时直接选择序号（从 0 开始）"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "smartbw_get_field",
        "description": "从 Vaultwarden 项目获取字段值。支持标准字段（password/username/uri/notes）和自定义字段，不区分大小写。多结果时可指定 index。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "项目名称搜索词"},
                "field": {"type": "string", "description": "字段名称，如 'token'、'endpoint'"},
                "index": {"type": "integer", "description": "多结果时直接选择序号（从 0 开始）"}
            },
            "required": ["name", "field"]
        }
    },
    {
        "name": "smartbw_get_item",
        "description": "获取 Vaultwarden 项目的完整信息，包括所有标准字段和自定义字段。多结果时列出选项，可指定 index 直接选取。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "项目名称搜索词"},
                "index": {"type": "integer", "description": "多结果时直接选择序号（从 0 开始）"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "smartbw_get_password",
        "description": "从 Vaultwarden 获取密码。自动模糊搜索项目名，多结果时列出选项，可指定 index 直接选取。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "项目名称搜索词"},
                "index": {"type": "integer", "description": "多结果时直接选择序号（从 0 开始）"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "smartbw_search",
        "description": "在 Vaultwarden 中模糊搜索项目，返回匹配列表。支持部分名称、错别字容错。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "limit": {"type": "integer", "description": "最大返回数（默认 5）", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "smartbw_list_all",
        "description": "列出 Vaultwarden 中的所有项目名称和用户名。用于浏览。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "smartbw_daemon_status",
        "description": "检查 smartbw-mcp 守护进程状态。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "smartbw_sync_cache",
        "description": "强制刷新缓存：先运行 bw sync 同步 Vaultwarden，再清除 MCP 内部项目缓存。当 Vaultwarden 中有新增/改名/删除项目但搜索不到时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
]


# === 每次新建 SmartBitwardenMCP 实例（UNIX socket 开销可忽略，确保数据新鲜） ===
_client_error_count = 0
_client_last_error = 0.0

def _get_client():
    """每次创建新的 SmartBitwardenMCP 实例。

    熔断保护：连续 3 次失败后进入 30s 冷却。
    """
    global _client_error_count, _client_last_error

    now = time.time()
    if _client_error_count >= 5 and now - _client_last_error < 30:
        wait = int(30 - now + _client_last_error)
        raise Exception(
            f"[分类D·熔断保护] smartbw-mcp 连续失败已达上限，{wait}s 后自动重试。\n"
            f"根因: 守护进程或 Vaultwarden 持续不可达"
        )

    try:
        c = SmartBitwardenMCP(use_daemon=True, timeout=30)
        if not c.initialize():
            raise Exception(
                "[分类A·守护进程未运行] smartbw-daemon 守护进程未启动"
            )
        _client_error_count = 0
        return c
    except Exception as e:
        _client_error_count += 1
        _client_last_error = now
        err_str = str(e).lower()

        # 根据错误内容判断具体原因
        if "守护进程未运行" in str(e) or "daemon 初始化失败" in str(e):
            raise Exception(
                f"[分类A·守护进程未运行] smartbw-daemon 守护进程连接失败。\n"
                f"根因: daemon 进程不存在或正在启动中\n"
                f"恢复: systemd 会在 10s 内自动重启，请稍后重试\n"
                f"手动: systemctl --user restart smartbw-daemon"
            )
        elif "session" in err_str or "unlock" in err_str or "locked" in err_str:
            bw_host = os.environ.get('BW_HOST', '未设置')
            raise Exception(
                f"[分类C·凭证问题] 守护进程无法解锁 Vaultwarden 金库。\n"
                f"根因: 主密码缺失/错误 或 Vaultwarden 服务器不可达\n"
                f"排查: 1) 检查 ~/.config/bitwarden-mcp/config.json 中 master_password 是否已加密\n"
                f"      2) 检查 ~/.config/bitwarden-mcp/.env 中 BW_MASTER_PASSWORD 是否存在\n"
                f"      3) 检查 Vaultwarden 服务器 {bw_host} 是否可达\n"
                f"      4) 查看 daemon 日志: tail -30 ~/.smartbw-mcp/daemon.log\n"
                f"     如有 NEEDS_REINIT 标记 → 需人工重新配置主密码"
            )
        elif "timeout" in err_str or "超时" in err_str:
            raise Exception(
                f"[分类B·服务超时] Vaultwarden 服务器响应超时，守护进程正在自动重试。\n"
                f"根因: Vaultwarden 服务暂时不可达或网络延迟\n"
                f"恢复: daemon 会自动重试（每 10s 一次，最多 5 次），通常 50s 内恢复\n"
                f"排查: tail -30 ~/.smartbw-mcp/daemon.log 查看解锁进度"
            )
        else:
            raise Exception(
                f"[分类未知] smartbw-mcp 连接异常: {e}\n"
                f"排查: systemctl --user status smartbw-daemon\n"
                f"      tail -30 ~/.smartbw-mcp/daemon.log"
            )


@contextmanager
def _get_client_ctx():
    """上下文管理器：确保 SmartBitwardenMCP 实例使用后自动关闭 socket。"""
    smart = None
    try:
        smart = _get_client()
        yield smart
    finally:
        if smart is not None:
            try:
                smart.close()
            except Exception:
                pass


# === MCP 协议处理 ===

def _handle_init(_id, _params):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "smartbw-mcp", "version": "2.3.0"}
    }


def _handle_tools_list(_id, _params):
    return {"tools": TOOLS}


def _item_to_info(item: dict) -> dict:
    """将 Vaultwarden 项目字典转为标准 info 结构"""
    login = item.get("login", {})
    fields = item.get("fields", [])
    return {
        "name": item.get("name", ""),
        "username": login.get("username", ""),
        "password": login.get("password", ""),
        "uris": [u.get("uri", "") for u in login.get("uris", [])],
        "notes": item.get("notes", ""),
        "fields": {f.get("name", ""): f.get("value", "") for f in fields} if fields else {},
    }


def _resolve_search(smart, search_term: str, index=None, max_results: int = 10) -> tuple:
    """搜索项目并处理 index 参数，返回 (item_dict, error_text)
    返回约定：err 为 None 时 item 必定有效；err 不为 None 时 item 为 None。
    """
    results = smart.fuzzy_search(search_term, max_results=max_results)
    if not results:
        return None, f"未找到 '{search_term}'"
    if len(results) == 1:
        item = smart.get_item_by_id(results[0].item.id)
        if item:
            return item, None
        return None, "获取项目详情失败"
    # 多结果：高置信度自动选取（首结果 score>=0.95 且与第二差距>=0.2）
    if len(results) >= 2:
        top_score = results[0].score
        second_score = results[1].score
        if top_score >= 0.95 and (top_score - second_score) >= 0.2:
            item = smart.get_item_by_id(results[0].item.id)
            if item:
                return item, None
    # 多结果
    if index is not None and 0 <= index < len(results):
        item = smart.get_item_by_id(results[index].item.id)
        if item:
            return item, None
        return None, "获取项目详情失败"
    # 多结果且无有效 index → 返回选项列表
    matches = [{"index": i, "name": r.item.name, "id": r.item.id[:8], "score": round(r.score, 2)}
               for i, r in enumerate(results)]
    return None, json.dumps({"matches": matches, "pick_one": True}, ensure_ascii=False, indent=2)


def _available_fields(item: dict) -> list:
    """提取项目的自定义字段名列表"""
    fields = item.get("fields", [])
    return [f.get("name", "") for f in fields if f.get("name")]


def _handle_tools_call(_id, params):
    name = params.get("name", "")
    args = params.get("arguments", {})

    # ── smartbw_get_api ──
    if name == "smartbw_get_api":
        try:
            with _get_client_ctx() as smart:
                search_term = args.get("name", "")
                index = args.get("index")
                item, err = _resolve_search(smart, search_term, index)
                if err:
                    # 可能是多结果选项列表
                    if err.startswith("{"):
                        return _text_result(err)
                    return _text_result(f"❌ {err}")
                assert item is not None  # err 为 None 时 item 必定有效
                # 查找 API 字段
                for f in item.get("fields", []):
                    if f.get("name", "").lower() == "api":
                        return _text_result(f.get("value", ""))
                # 未找到 API 字段 → 返回结构化信息
                fields = _available_fields(item)
                return _text_result(json.dumps({
                    "found": False,
                    "item": item.get("name", search_term),
                    "available_fields": fields,
                    "hint": "API 字段不存在，使用 smartbw_get_field 获取其他字段"
                }, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.exception(f"smartbw_get_api 处理异常: {e}")
            return _text_result(f"❌ {e}", is_error=True)

    # ── smartbw_get_field ──
    elif name == "smartbw_get_field":
        fname = args.get("field", "")
        if not fname:
            return _text_result("❌ 缺少字段名参数")
        try:
            with _get_client_ctx() as smart:
                search_term = args.get("name", "")
                index = args.get("index")
                item, err = _resolve_search(smart, search_term, index)
                if err:
                    if err.startswith("{"):
                        return _text_result(err)
                    return _text_result(f"❌ {err}")
                assert item is not None  # err 为 None 时 item 必定有效

            fname_lower = fname.lower()
            login = item.get("login", {})

            # 1) 检查标准字段（login 对象 + notes）
            if fname_lower == "password":
                pwd = login.get("password", "")
                if pwd:
                    return _text_result(pwd)
            elif fname_lower in ("username", "user", "login"):
                usr = login.get("username", "")
                if usr:
                    return _text_result(usr)
            elif fname_lower in ("uri", "url", "link"):
                uris = login.get("uris", [])
                if uris:
                    return _text_result(uris[0].get("uri", ""))
            elif fname_lower == "notes":
                notes = item.get("notes", "")
                if notes:
                    return _text_result(notes)

            # 2) 检查自定义字段
            for f in item.get("fields", []):
                if f.get("name", "").lower() == fname_lower:
                    return _text_result(f.get("value", ""))

            # 未找到字段 → 收集所有可用字段（标准 + 自定义）
            all_fields = []
            if login.get("password"):
                all_fields.append("password")
            if login.get("username"):
                all_fields.append("username")
            if login.get("uris"):
                all_fields.append("uri")
            if item.get("notes"):
                all_fields.append("notes")
            all_fields.extend(_available_fields(item))

            return _text_result(json.dumps({
                "found": False,
                "item": item.get("name", search_term),
                "field": fname,
                "available_fields": all_fields,
                "hint": f"字段 '{fname}' 不存在，以上是可用字段（含标准字段和自定义字段）"
            }, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.exception(f"smartbw_get_field 处理异常: {e}")
            return _text_result(f"❌ {e}", is_error=True)

    # ── smartbw_get_item ──
    elif name == "smartbw_get_item":
        search_term = args.get("name", "")
        index = args.get("index")
        try:
            with _get_client_ctx() as smart:
                item, err = _resolve_search(smart, search_term, index, max_results=10)
                if err:
                    if err.startswith("{"):
                        return _text_result(err)
                    return _text_result(f"❌ {err}")
                assert item is not None
                info = _item_to_info(item)
                return _text_result(json.dumps(info, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.exception(f"smartbw_get_item 处理异常: {e}")
            return _text_result(f"❌ {e}", is_error=True)

    # ── smartbw_get_password ──
    elif name == "smartbw_get_password":
        search_term = args.get("name", "")
        index = args.get("index")
        try:
            with _get_client_ctx() as smart:
                item, err = _resolve_search(smart, search_term, index, max_results=10)
                if err:
                    if err.startswith("{"):
                        return _text_result(err)
                    return _text_result(f"❌ {err}")
                assert item is not None
                login = item.get("login", {})
                password = login.get("password", "")
                if password:
                    return _text_result(password)
                return _text_result("❌ 该项目无密码")
        except Exception as e:
            logger.exception(f"smartbw_get_password 处理异常: {e}")
            return _text_result(f"❌ {e}", is_error=True)

    # ── smartbw_search ──
    elif name == "smartbw_search":
        query = args.get("query", "")
        limit = min(args.get("limit", 5), 20)
        if not query:
            return _text_result("❌ 缺少搜索词")
        try:
            with _get_client_ctx() as smart:
                results = smart.fuzzy_search(query, max_results=limit)
                if not results:
                    return _text_result("无结果")
                lines = [f"找到 {len(results)} 个结果:"]
                for i, r in enumerate(results):
                    lines.append(f"  [{i}] {r.item.name} | user={r.item.username or '(无)'} | score={r.score:.2f}")
                return _text_result("\n".join(lines))
        except Exception as e:
            logger.exception(f"smartbw_search 处理异常: {e}")
            return _text_result(f"❌ 搜索失败: {e}", is_error=True)

    # ── smartbw_list_all ──
    elif name == "smartbw_list_all":
        try:
            with _get_client_ctx() as smart:
                items = smart.list_all_items()
                if not items:
                    return _text_result("无项目")
                lines = [f"共 {len(items)} 个项目:"]
                for i, item in enumerate(items):
                    lines.append(f"  [{i}] {item.name} | user={item.username or '(无)'}")
                return _text_result("\n".join(lines))
        except Exception as e:
            logger.exception(f"smartbw_list_all 处理异常: {e}")
            return _text_result(f"❌ 获取列表失败: {e}", is_error=True)

    # ── smartbw_daemon_status ──
    elif name == "smartbw_daemon_status":
        pid_file = Path.home() / ".smartbw-mcp" / "daemon.pid"
        sock = Path.home() / ".smartbw-mcp" / "daemon.sock"
        if pid_file.exists() and sock.exists():
            pid = pid_file.read_text().strip()
            try:
                os.kill(int(pid), 0)
                return _text_result(f"✅ 运行中 (PID {pid})")
            except (OSError, ValueError):
                return _text_result(f"⚠️ PID 文件存在但进程已死 (pid={pid})")
        return _text_result("❌ 未运行")

    # ── smartbw_sync_cache ──
    elif name == "smartbw_sync_cache":
        try:
            import random
            import socket
            import subprocess

            # Step 1: bw sync
            env = os.environ.copy()
            env["BW_HOST"] = get_config().get("bw_host", "")
            r = subprocess.run(["bw", "sync"], env=env, capture_output=True, text=True, timeout=30)
            sync_ok = r.returncode == 0
            sync_msg = r.stdout.strip() if sync_ok else r.stderr.strip()

            # Step 2: restart daemon's node MCP server
            daemon_msg = ""
            sock_path = Path.home() / ".smartbw-mcp" / "daemon.sock"
            if sock_path.exists():
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(10)
                    sock.connect(str(sock_path))
                    req = {"jsonrpc": "2.0", "id": random.randint(1, 999999),
                           "method": "daemon/restart-mcp", "params": {}}
                    sock.sendall((json.dumps(req) + "\n").encode())
                    buf = b""
                    while b"\n" not in buf:
                        buf += sock.recv(4096)
                    resp = json.loads(buf.partition(b"\n")[0])
                    if "result" in resp:
                        daemon_msg = "✅ 守护进程 MCP Server 已重启"
                    else:
                        daemon_msg = f"⚠️ 守护进程重启失败: {resp.get('error', {}).get('message', '?')}"
                except Exception as e:
                    daemon_msg = f"⚠️ 无法重启守护进程 MCP Server: {e}"
                finally:
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass

            # Step 3: clear internal Python cache

            lines = [
                f"bw sync: {'✅ ' + sync_msg if sync_ok else '❌ ' + sync_msg}",
                daemon_msg,
                "MCP Python 缓存已清除"
            ]
            return _text_result("\n".join(lines), is_error=not sync_ok)
        except Exception as e:
            logger.exception(f"smartbw_sync_cache 处理异常: {e}")
            return _text_result(f"❌ 刷新失败: {e}", is_error=True)

    return _text_result(f"未知工具: {name}", is_error=True)


def _text_result(text: str, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        try:
            if method == "initialize":
                result = _handle_init(req_id, params)
            elif method == "notifications/initialized":
                continue  # no response needed
            elif method == "tools/list":
                result = _handle_tools_list(req_id, params)
            elif method == "tools/call":
                result = _handle_tools_call(req_id, params)
            else:
                result = _text_result(f"不支持的方法: {method}", is_error=True)

            response = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            logger.exception(f"main 处理请求失败: {e}")
            response = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32603, "message": str(e)}}

        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

        # 优雅退出：检测到信号文件后处理完当前请求即退出
        if SHUTDOWN_SIGNAL.exists():
            try:
                SHUTDOWN_SIGNAL.unlink()
            except OSError:
                pass
            sys.exit(0)


if __name__ == "__main__":
    main()
