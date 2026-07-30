"""
智能搜索模块

负责:
- 模糊搜索辅助函数(_normalize, _fuzzy_score)
- SmartBitwardenMCP 类(缓存、模糊搜索、智能密码获取)
- 单例工厂函数(get_smart_mcp, get_password_smart)
- CLI 入口(main)
"""
import difflib
import json
import logging
import sys
import threading
import time
from typing import Dict, List, Optional

from config import DEFAULT_TIMEOUT, FUZZY_THRESHOLD, logger
from mcp_raw import RealMCPClient
from models import BwItem, LockedError, SearchResult, TimeoutError

# ============================================================================
# 智能搜索辅助函数
# ============================================================================

def _normalize(s: Optional[str]) -> str:
    if s is None:
        return ""
    return s.lower().replace("_", " ").replace("-", " ").replace(".", " ").strip()


def _fuzzy_score(query: str, target: str) -> float:
    q = _normalize(query)
    t = _normalize(target)

    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    if q in t:
        # 极短查询（<3 字符）容易产生假阳性匹配
        return 0.85 if len(q) >= 3 else 0.50
    # 避免短目标出现在长查询中产生假阳性（如 "x" in "xyz98765" → 0.80）
    # 要求目标长度至少是查询长度的 40% 才算有意义的匹配
    if t in q and len(t) >= len(q) * 0.4:
        return 0.80

    return difflib.SequenceMatcher(None, q, t).ratio()


# ============================================================================
# 智能 Bitwarden MCP 客户端
# ============================================================================

class SmartBitwardenMCP:
    """智能 Bitwarden MCP 客户端"""

    def __init__(self, auto_init: bool = True,
                 timeout: int = DEFAULT_TIMEOUT, use_daemon: bool = True):
        self.client = RealMCPClient(timeout, use_daemon=use_daemon)

        # 缓存机制（TTL 300秒，密码库不会秒变）
        self._items_cache = None
        self._cache_time = 0
        self._cache_ttl = 30  # 30 秒 TTL，减少缓存延迟

        # 搜索索引（首次加载后建，加速后续查询）
        self._name_index: Dict[str, List[Dict]] = {}  # normalized_name → [item_dict, ...]
        self._index_dirty = True

        if auto_init:
            self.initialize()

    def initialize(self) -> bool:
        return self.client.initialize()

    def health_check(self) -> bool:
        """健康检查"""
        return self.client.ping()

    def clear_cache(self) -> None:
        """清除项目缓存和索引"""
        self._items_cache = None
        self._cache_time = 0
        self._name_index = {}
        self._index_dirty = True
        logger.info("缓存和索引已清除")

    def _build_index(self) -> None:
        """构建搜索索引（name→item 映射，支持快速前缀/精确查找）"""
        if not self._index_dirty or self._items_cache is None:
            return
        self._name_index = {}
        for item in self._items_cache:
            name = _normalize(item.get("name", ""))
            if name not in self._name_index:
                self._name_index[name] = []
            self._name_index[name].append(item)
        self._index_dirty = False
        logger.info(f"索引构建完成: {len(self._name_index)} 个唯一条目名")

    def list_all_items(self) -> List[BwItem]:
        """
        列出所有项目
        返回所有项目的列表(使用缓存)
        """
        # 使用缓存机制
        current_time = time.time()
        if (self._items_cache is None or
            current_time - self._cache_time > self._cache_ttl):
            try:
                # 缓存过期：先 sync 确保 node MCP server 拿到最新数据
                self.client.call_tool("sync", {})
                self._items_cache = self.client.list_items(type="items")
                self._cache_time = current_time
                logger.info(f"缓存更新,项目数: {len(self._items_cache)}")
            except TimeoutError:
                logger.error("list_all_items 超时")
                return []
            except LockedError:
                logger.error("list_all_items 锁定失败")
                return []

        items_dict = self._items_cache
        items = []
        for item_dict in items_dict:
            items.append(BwItem(
                id=item_dict.get("id", ""),
                name=item_dict.get("name", ""),
                username=item_dict.get("login", {}).get("username", ""),
                password="",  # 不预加载密码
                uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])],
                notes=item_dict.get("notes", "")
            ))
        return items

    def update_item(self, item_id: str, name: Optional[str] = None, username: Optional[str] = None,
                    password: Optional[str] = None, uri: Optional[str] = None, notes: Optional[str] = None) -> bool:
        """更新项目,返回是否成功"""
        try:
            return self.client.update_item(item_id, name, username, password, uri, notes)
        except TimeoutError:
            logger.error("update_item 超时")
            return False
        except LockedError:
            logger.error("update_item 锁定失败")
            return False
        except Exception:
            logger.error("update_item 失败", exc_info=True)
            return False

    def delete_item(self, item_id: str) -> bool:
        """删除项目,返回是否成功"""
        try:
            return self.client.delete_item(item_id)
        except TimeoutError:
            logger.error("delete_item 超时")
            return False
        except LockedError:
            logger.error("delete_item 锁定失败")
            return False
        except Exception:
            logger.error("delete_item 失败", exc_info=True)
            return False

    def get_item_by_id(self, item_id: str) -> Optional[Dict]:
        """
        根据 ID 获取完整项目信息
        返回项目详情字典
        """
        try:
            return self.client.get_item(item_id)
        except TimeoutError:
            logger.error("get_item_by_id 超时")
            return None
        except LockedError:
            logger.error("get_item_by_id 锁定失败")
            return None

    def search_items(self, query: str, max_results: int = 10, retry_on_empty: bool = True) -> List[SearchResult]:
        """
        搜索项目(支持自定义字段,返回更多结果)

        参数:
            query: 搜索词
            max_results: 最大返回结果数(默认10)
            retry_on_empty: 如果首次搜索无结果,是否自动清除缓存重试一次
        """
        logger.info(f"搜索项目: '{query}' (max={max_results})")

        # 第一次搜索
        results = self._do_fuzzy_search(query, max_results)

        # 如果无结果且允许重试,且缓存存在(说明可能是缓存过期),清除缓存重试一次
        if not results and retry_on_empty and self._items_cache is not None:
            logger.info(f"搜索 '{query}' 无结果,尝试清除缓存重试...")
            print(f"🔍 搜索 '{query}' 无项目,正在刷新缓存后重试,请稍候...")
            self.clear_cache()
            results = self._do_fuzzy_search(query, max_results)
            if results:
                logger.info(f"重试成功,找到 {len(results)} 个结果")
            else:
                logger.info(f"重试后仍无结果")

        return results

    def fuzzy_search(self, query: str, max_results: int = 5, retry_on_empty: bool = True) -> List[SearchResult]:
        """
        模糊搜索(支持自定义字段)

        参数:
            query: 搜索词
            max_results: 最大返回结果数
            retry_on_empty: 如果首次搜索无结果,是否自动清除缓存重试一次
        """
        logger.info(f"模糊搜索: '{query}'")
        return self.search_items(query, max_results, retry_on_empty)

    def _do_fuzzy_search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """执行实际的模糊搜索（内部方法，带索引加速）"""
        # 使用缓存机制
        current_time = time.time()
        if (self._items_cache is None or
            current_time - self._cache_time > self._cache_ttl):
            try:
                # 缓存过期：先 sync 确保 node MCP server 拿到最新数据
                self.client.call_tool("sync", {})
                self._items_cache = self.client.list_items(type="items")
                self._cache_time = current_time
                self._index_dirty = True
                logger.info(f"缓存更新,项目数: {len(self._items_cache)}")
            except TimeoutError:
                logger.error("获取列表超时,尝试重新初始化")
                self.client.close()
                time.sleep(0.5)
                try:
                    self.initialize()
                    self._items_cache = self.client.list_items(type="items")
                    self._cache_time = current_time
                    self._index_dirty = True
                except TimeoutError:
                    logger.error("重试 list_items 再次超时, Vaultwarden 不可达")
                    self._items_cache = self._items_cache or []
                    # 空缓存时重置 _cache_time 避免 TTL 空窗屏蔽后续搜索
                    if not self._items_cache:
                        self._cache_time = 0

        items = self._items_cache
        if not items:
            return []

        # 构建索引（如果脏了）
        self._build_index()

        # 快速路径：索引精确/前缀匹配
        norm_q = _normalize(query)
        exact_matches = self._name_index.get(norm_q, [])
        prefix_matches = []
        for name, name_items in self._name_index.items():
            if name.startswith(norm_q) and name != norm_q:
                prefix_matches.extend(name_items)
        indexed_items = exact_matches + prefix_matches

        # 如果索引命中足够，直接返回
        if len(indexed_items) >= max_results:
            results = []
            for item_dict in indexed_items[:max_results]:
                results.append(SearchResult(
                    item=BwItem(
                        id=item_dict.get("id", ""),
                        name=item_dict.get("name", ""),
                        username=item_dict.get("login", {}).get("username", ""),
                        password="",
                        uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])],
                        notes=item_dict.get("notes", "")
                    ),
                    score=1.0,
                    matched_field="name"
                ))
            logger.info(f"索引快速命中: {len(results)} 结果")
            return results

        # 索引已命中的 ID 集合，避免模糊搜索重复
        indexed_ids = {it.get("id") for it in indexed_items}

        scored = []
        for item_dict in items:
            # 跳过索引已覆盖的条目
            if item_dict.get("id") in indexed_ids:
                continue

            name = item_dict.get("name", "")
            username = item_dict.get("login", {}).get("username", "")
            fields = item_dict.get("fields", [])

            # 计算匹配度:名称、用户名、字段名、字段值
            name_score = _fuzzy_score(query, name)
            user_score = _fuzzy_score(query, username)
            best_score = max(name_score, user_score)
            matched_field = "name" if name_score >= user_score else "username"

            # 搜索自定义字段
            for field in fields:
                field_name = field.get("name", "")
                field_value = field.get("value", "")
                field_type = field.get("type", 0)  # 0=text, 1=hidden, 2=boolean

                # 字段名匹配权重更高
                field_name_score = _fuzzy_score(query, field_name)
                field_value_score = _fuzzy_score(query, field_value)
                field_score = max(field_name_score * 1.2, field_value_score * 0.8)

                if field_score > best_score:
                    best_score = field_score
                    matched_field = f"field:{field_name}"

            # 使用提高后的阈值 0.5,减少无关结果
            if best_score >= FUZZY_THRESHOLD:
                bw_item = BwItem(
                    id=item_dict.get("id", ""),
                    name=name,
                    username=username,
                    uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])]
                )

                scored.append(SearchResult(
                    item=bw_item,
                    score=best_score,
                    matched_field=matched_field
                ))

        # 将索引命中项也转为 SearchResult
        indexed_results = []
        for item_dict in indexed_items:
            indexed_results.append(SearchResult(
                item=BwItem(
                    id=item_dict.get("id", ""),
                    name=item_dict.get("name", ""),
                    username=item_dict.get("login", {}).get("username", ""),
                    uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])]
                ),
                score=1.0,
                matched_field="name"
            ))

        # 合并索引命中项 + 模糊评分项，统一按分数排序（一次排够）
        all_results = indexed_results + scored
        all_results.sort(key=lambda x: x.score, reverse=True)
        results = all_results[:max_results]
        logger.info(f"搜索完成: '{query}' -> {len(results)} 个结果 (包含字段搜索)")
        return results

    def get_password_smart(self, search_term: str, max_retries: int = 2) -> Optional[str]:
        """智能获取密码"""
        last_error = None
        best = None

        for attempt in range(max_retries):
            try:
                results = self.fuzzy_search(search_term, max_results=3)
                if not results:
                    if attempt < max_retries - 1:
                        logger.warning(f"第 {attempt+1} 次搜索无结果,重试...")
                        self.client.close()
                        time.sleep(0.5)
                        self.initialize()
                        continue
                    logger.info(f"搜索 '{search_term}' 无结果")
                    return None

                best = results[0]
                if best.score < 0.5:
                    logger.warning(f"匹配度较低 ({best.score:.2f}),但仍尝试: {best.item.name}")

                password = self.client.get_password(best.item.id)
                if password:
                    logger.info(f"找到 '{best.item.name}' (匹配度: {best.score:.2f})")
                    return password

                # 密码为空但找到了记录
                if attempt < max_retries - 1:
                    logger.warning(f"密码为空,重试...")
                    self.client.close()
                    time.sleep(0.5)
                    self.initialize()
                    continue

            except LockedError:
                if best is not None:
                    # get_password 已内置自动解锁,重试
                    password = self.client.get_password(best.item.id)
                    if password:
                        logger.info(f"找到 '{best.item.name}' (解锁后)")
                        return password
                raise
            except TimeoutError as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(f"第 {attempt+1} 次尝试超时: {e},重试...")
                    self.client.close()
                    time.sleep(1)
                    self.initialize()
                    continue
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(f"第 {attempt+1} 次尝试失败: {e},重试...")
                    self.client.close()
                    time.sleep(1)
                    self.initialize()
                    continue

        if last_error:
            logger.error(f"最终失败: {last_error}")
        return None

    def list_all(self) -> List[BwItem]:
        """列出所有项目(使用缓存)，别名 list_all_items"""
        return self.list_all_items()

    def get_field(self, search_term: str, field_name: str) -> Optional[str]:
        """获取指定自定义字段值"""
        results = self.fuzzy_search(search_term, max_results=3)
        if not results:
            return None
        best = results[0]
        item = self.client.get_item(best.item.id)
        if not item:
            return None
        for f in item.get("fields", []):
            if f.get("name", "").lower() == field_name.lower():
                return f.get("value", "")
        return None

    def get_api_key(self, search_term: str) -> Optional[str]:
        """获取 API Key（等同于 get_field(name, 'API')）"""
        return self.get_field(search_term, "API")

    def get_username(self, search_term: str) -> Optional[str]:
        """获取用户名"""
        results = self.fuzzy_search(search_term, max_results=3)
        if not results:
            return None
        return results[0].item.username or None

    def get_uri(self, search_term: str) -> Optional[str]:
        """获取 URI"""
        results = self.fuzzy_search(search_term, max_results=3)
        if not results:
            return None
        uris = results[0].item.uris
        return uris[0] if uris else None

    def get_notes(self, search_term: str) -> Optional[str]:
        """获取备注"""
        results = self.fuzzy_search(search_term, max_results=3)
        if not results:
            return None
        item = self.client.get_item(results[0].item.id)
        return item.get("notes") or None if item else None

    def close(self):
        self.client.close()


# ============================================================================
# 简化接口
# ============================================================================

_singleton: Optional[SmartBitwardenMCP] = None
_singleton_lock = threading.Lock()


def get_smart_mcp(timeout: int = DEFAULT_TIMEOUT, reset: bool = False) -> SmartBitwardenMCP:
    """
    获取单例实例（线程安全）

    参数:
        timeout: 超时时间（仅在创建新实例时使用）
        reset:   强制重置单例，关闭旧连接并创建新实例
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None or reset:
            if _singleton is not None:
                try:
                    _singleton.client.close()
                except Exception:
                    pass
            _singleton = SmartBitwardenMCP(timeout=timeout)
        return _singleton


def get_password_smart(search_term: str) -> Optional[str]:
    """智能获取密码 - 最简单接口"""
    client = get_smart_mcp()
    return client.get_password_smart(search_term)


# ============================================================================
# CLI 接口
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="真正的 Bitwarden MCP 客户端")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    get_parser = subparsers.add_parser("get", help="智能获取密码")
    get_parser.add_argument("search", help="搜索词")
    get_parser.add_argument("--session", help="BW_SESSION token")
    get_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})")

    search_parser = subparsers.add_parser("search", help="搜索项目")
    search_parser.add_argument("query", help="搜索词")
    search_parser.add_argument("--session", help="BW_SESSION token")
    search_parser.add_argument("--limit", type=int, default=3, help="显示数量")
    search_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})")

    list_parser = subparsers.add_parser("list", help="列出项目")
    list_parser.add_argument("--session", help="BW_SESSION token")
    list_parser.add_argument("--all", action="store_true", help="列出所有")
    list_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})")

    test_parser = subparsers.add_parser("test", help="测试连接")
    test_parser.add_argument("--session", help="BW_SESSION token")
    test_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})")

    ping_parser = subparsers.add_parser("ping", help="健康检查")
    ping_parser.add_argument("--session", help="BW_SESSION token")
    ping_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    timeout = getattr(args, 'timeout', DEFAULT_TIMEOUT)
    client = SmartBitwardenMCP(timeout=timeout)

    try:
        if args.command == "get":
            password = client.get_password_smart(args.search)
            if password:
                print(password)
            else:
                print("❌ 未找到")
                sys.exit(1)

        elif args.command == "search":
            results = client.fuzzy_search(args.query, max_results=args.limit)
            for i, result in enumerate(results, 1):
                print(f"{i}. [{result.score:.3f}] {result.item.name}")
                print(f"   用户: {result.item.username}")
                print(f"   ID: {result.item.id}")
                print()

        elif args.command == "list":
            items = client.list_all()
            for i, item in enumerate(items[:20], 1):
                print(f"{i}. {item.name}")
                print(f"   用户: {item.username}")
                print(f"   ID: {item.id}")
                if item.uris:
                    print(f"   URL: {item.uris[0]}")
                print()
            print(f"共 {len(items)} 个项目(显示前 20 个)")

        elif args.command == "test":
            if client.initialize():
                print("✅ MCP 连接正常")
                items = client.list_all()
                print(f"   项目数: {len(items)}")
                if items:
                    print(f"   示例: {items[0].name} - {items[0].username}")
            else:
                print("❌ MCP 连接失败")
                sys.exit(1)

        elif args.command == "ping":
            if client.health_check():
                print("✅ MCP 服务健康")
            else:
                print("❌ MCP 服务不健康")
                sys.exit(1)

    except KeyboardInterrupt:
        print("\n操作取消")
        sys.exit(130)
    except TimeoutError as e:
        print(f"❌ 超时: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


# ============================================================================

if __name__ == "__main__":
    main()
