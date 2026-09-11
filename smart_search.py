"""
智能搜索模块

负责:
- 模糊搜索辅助函数(_normalize, _fuzzy_score)
- SmartBitwardenMCP 类(缓存、模糊搜索、智能密码获取)
- 单例工厂函数(get_smart_mcp, get_password_smart)
- CLI 入口(main)
"""

import difflib
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from config import (
    CACHE_REFRESH_MIN_AGE,
    CACHE_SUSPICIOUS_SCORE,
    CACHE_TTL,
    DEFAULT_TIMEOUT,
    FUZZY_THRESHOLD,
    logger,
)
from mcp_raw import RealMCPClient
from models import (
    BwConnectionError,
    BwItem,
    BwTimeoutError,
    LockedError,
    SearchResult,
)

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


def _fetch_items(client) -> List[Dict]:
    """拉取最新项目列表：先 sync（唯一的服务器请求）再全量 list。

    实测 list 的开销是本地全量解密（~3.3s 固定），带不带 search 过滤都一样，
    所以这里不做 search 窄化 —— 靠"短 TTL 缓存 + 后台刷新"来摊薄，而不是缩短单次拉取。
    """
    client.call_tool("sync", {})
    return client.list_items(type="items") or []


# ============================================================================
# 智能 Bitwarden MCP 客户端
# ============================================================================


class SmartBitwardenMCP:
    """智能 Bitwarden MCP 客户端"""

    def __init__(
        self, auto_init: bool = True, timeout: int = DEFAULT_TIMEOUT, use_daemon: bool = True
    ):
        self.client = RealMCPClient(timeout, use_daemon=use_daemon)

        # 长驻缓存：TTL 见 config.CACHE_TTL（默认 15s）。
        # 后端多为个人自建 Vaultwarden，TTL 必须短，否则条目更新后会查到旧值。
        # 注意：只有在实例跨调用复用时这个缓存才生效（见 smartbw_mcp_server._get_client）。
        self._items_cache: Optional[List[Dict]] = None
        self._cache_time = 0.0
        self._cache_ttl = CACHE_TTL
        self._last_attempt_time = 0.0

        # O4 single-flight：前台装载与异步刷新共用，保证同一时刻只有一次真正拉取
        self._load_lock = threading.Lock()
        # 异步刷新状态（仅 smartbw_sync_cache 清缓存后预热用；
        # TTL 过期走 _ensure_items 的同步刷新，不经过这里）
        self._refresh_inflight = False
        self._refresh_lock = threading.Lock()

        # 搜索索引（随缓存整体替换，加速后续查询）
        self._name_index: Dict[str, List[Dict]] = {}  # normalized_name → [item_dict, ...]
        # 原子快照：(items, index) 一次绑定，读侧永远看到一致的一对（P1-5）
        self._snapshot: Optional[Tuple[List[Dict], Dict[str, List[Dict]]]] = None

        # O6 统计
        self._stat_hits = 0
        self._stat_rebuilds = 0
        self._stat_bg_refresh = 0
        self._stat_force_refresh = 0

        if auto_init:
            self.initialize()

    def initialize(self) -> bool:
        return self.client.initialize()

    def health_check(self) -> bool:
        """健康检查"""
        return self.client.ping()

    # ─── 缓存管理 ─────────────────────────────

    def clear_cache(self) -> None:
        """清除项目缓存与索引（下次查询重新装载，确保拿到最新数据）"""
        self._snapshot = None
        self._items_cache = None
        self._cache_time = 0.0
        self._name_index = {}
        logger.info("[cache] 已清除缓存与索引")

    def refresh_stats(self) -> str:
        """O6：缓存命中/重建统计，供日志与 smartbw_sync_cache 展示"""
        return (
            f"命中={self._stat_hits} 同步重建={self._stat_rebuilds} "
            f"异步预热={self._stat_bg_refresh} 强制刷新={self._stat_force_refresh}"
        )

    def _cache_age(self) -> float:
        return (time.time() - self._cache_time) if self._cache_time else float("inf")

    def _is_stale(self) -> bool:
        return self._items_cache is None or self._cache_age() > self._cache_ttl

    def _should_force_refresh(self) -> bool:
        """O3：距最近一次装载（成功装载完成 或 失败尝试）太近就跳过。

        必须取 max(_cache_time, _last_attempt_time)：
        - _cache_time 在**装载完成**时写入 → 刚装载完不会立刻再拉（避免白跑两遍）
        - _last_attempt_time 在**尝试开始**时写入 → 装载失败时也能拦住连环重试
        只用 _last_attempt_time 是有坑的：一次装载耗时约 5s，与该门槛同量级，
        等它返回时门槛早已到期，导致"刚装载完立刻又拉一遍"。
        """
        last = max(self._cache_time, self._last_attempt_time)
        return (time.time() - last) >= CACHE_REFRESH_MIN_AGE

    def _install_items(self, items: List[Dict]) -> None:
        """整体替换缓存与索引。

        关键：`(items, index)` 必须以**一次绑定**替换 —— 读侧会同时使用两者，
        分两次赋值会让读侧看到「新 items + 旧 index」的撕裂快照（P1-5）。
        下面三个属性是镜像/便利访问，供既有读侧与测试使用。
        """
        index: Dict[str, List[Dict]] = {}
        for item in items:
            index.setdefault(_normalize(item.get("name", "")), []).append(item)
        self._snapshot = (items, index)  # 原子：单次属性绑定
        self._items_cache = items
        self._name_index = index
        self._cache_time = time.time()

    def _snapshot_items(self) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
        """原子读取一致的 (items, index) 快照。"""
        snap = self._snapshot
        if snap is None:
            return self._items_cache or [], self._name_index
        return snap

    def _fetch_with_temp_client(self) -> List[Dict]:
        """用独立连接拉取数据。

        不能在后台线程复用 self.client —— 主线程每次工具调用结束都会 close() 它的
        socket，并发时会互相打断。多开一条 unix socket 的代价可忽略。
        """
        tmp = RealMCPClient(self.client.timeout, use_daemon=True)
        try:
            if not tmp.initialize():
                raise BwConnectionError("守护进程不可用，无法拉取缓存")
            return _fetch_items(tmp)
        finally:
            try:
                tmp.close()
            except Exception:
                pass

    def _load_items_blocking(self, force: bool = False) -> List[Dict]:
        """阻塞式装载（O4：并发时只有一个线程真正去拉，其余等它完成后复用结果）。"""
        with self._load_lock:
            if not force and self._items_cache is not None and not self._is_stale():
                self._stat_hits += 1
                return self._items_cache
            self._last_attempt_time = time.time()
            t0 = time.time()
            try:
                items = self._fetch_with_temp_client()
            except Exception as e:
                # 失败时保留旧缓存；不更新 _cache_time，下次查询会重试
                logger.error(f"[cache] 装载失败: {e}")
                return self._items_cache or []
            self._install_items(items)
            if force:
                self._stat_force_refresh += 1
            else:
                self._stat_rebuilds += 1
            logger.info(
                f"[cache] 装载完成: {len(items)} 条, 耗时 {time.time() - t0:.2f}s"
                f" | {self.refresh_stats()}"
            )
            return self._items_cache or []

    def _maybe_background_refresh(self) -> None:
        """异步刷新缓存（不阻塞调用方）。

        仅在 smartbw_sync_cache 清缓存后用于预热，避免下一条查询又等一次全量装载。
        常规查询的 TTL 过期走 _ensure_items 的同步刷新，不经过这里。
        """
        with self._refresh_lock:
            if self._refresh_inflight:
                return
            self._refresh_inflight = True
        threading.Thread(target=self._background_refresh_worker, daemon=True).start()

    def _background_refresh_worker(self) -> None:
        try:
            with self._load_lock:  # 与前台装载 single-flight，避免重复拉取
                self._last_attempt_time = time.time()
                t0 = time.time()
                try:
                    items = self._fetch_with_temp_client()
                except Exception as e:
                    logger.warning(f"[cache] 异步刷新失败: {e}")
                    return
                self._install_items(items)
                self._stat_bg_refresh += 1
                logger.info(
                    f"[cache] 异步刷新完成: {len(items)} 条, 耗时 {time.time() - t0:.2f}s"
                    f" | {self.refresh_stats()}"
                )
        finally:
            with self._refresh_lock:
                self._refresh_inflight = False

    def _ensure_items(self, force: bool = False) -> List[Dict]:
        """取项目列表（甲方案：无定时器，纯被动按需刷新）。

        - 命中（缓存年龄 ≤ TTL）→ 直接读缓存，0 次请求
        - 过期（年龄 > TTL）    → **同步刷新后再回答**，保证本次数据是最新的
        - 冷启动（无缓存）      → 同步装载
        - force=True            → 强制同步重建（用于无结果 / 结果可疑的重查）

        注意：这里没有任何定时器。无人查询时不会产生任何 Vaultwarden 流量。
        """
        if force:
            return self._load_items_blocking(force=True)
        if self._items_cache is None or self._is_stale():
            return self._load_items_blocking()
        self._stat_hits += 1
        return self._items_cache or []

    def warm_up(self) -> None:
        """O5：预热缓存（可在后台线程调用），避免首次查询等一次全量装载。"""
        self._ensure_items()

    def refresh_async(self) -> None:
        """异步刷新缓存，调用方不阻塞（供 smartbw_sync_cache 清缓存后预热）。"""
        self._maybe_background_refresh()

    def list_all_items(self) -> List[BwItem]:
        """列出所有项目（使用长驻缓存，TTL 见 config.CACHE_TTL）"""
        items_dict = self._ensure_items()
        items = []
        for item_dict in items_dict:
            items.append(
                BwItem(
                    id=item_dict.get("id", ""),
                    name=item_dict.get("name", ""),
                    username=item_dict.get("login", {}).get("username", ""),
                    password="",  # 不预加载密码
                    uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])],
                    notes=item_dict.get("notes", ""),
                )
            )
        return items

    def update_item(
        self,
        item_id: str,
        name: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        uri: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """更新项目,返回是否成功"""
        try:
            return self.client.update_item(item_id, name, username, password, uri, notes)
        except BwTimeoutError:
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
        except BwTimeoutError:
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
        except BwTimeoutError:
            logger.error("get_item_by_id 超时")
            return None
        except LockedError:
            logger.error("get_item_by_id 锁定失败")
            return None

    def search_items(
        self, query: str, max_results: int = 10, retry_on_empty: bool = True
    ) -> List[SearchResult]:
        """
        搜索项目(支持自定义字段,返回更多结果)

        参数:
            query: 搜索词
            max_results: 最大返回结果数(默认10)
            retry_on_empty: 无结果 / 结果可疑时，是否强制刷新缓存后重查一次

        刷新策略（专治"该刷没刷"，条目已更新却查到旧值）：
          - 无结果     → 强制刷新后重查
          - 结果可疑   → 最高分低于 CACHE_SUSPICIOUS_SCORE 时强制刷新后重查，取两次里更优的
          - 受 O3 门槛约束：距上次装载不足 CACHE_REFRESH_MIN_AGE 秒则跳过，避免白跑
        注意：这里只用 logger（stderr），绝不 print —— MCP server 的 stdout 是 JSON-RPC 流。
        """
        logger.info(f"搜索项目: '{query}' (max={max_results})")

        results = self._do_fuzzy_search(query, max_results)

        need_refresh = False
        reason = ""
        if not results and retry_on_empty:
            need_refresh, reason = True, "无结果"
        elif results and results[0].score < CACHE_SUSPICIOUS_SCORE:
            need_refresh, reason = True, f"最高分 {results[0].score:.2f} 偏低"

        if need_refresh:
            if not self._should_force_refresh():
                logger.info(
                    f"搜索 '{query}' {reason}，但距上次装载不足 "
                    f"{CACHE_REFRESH_MIN_AGE}s，跳过刷新"
                )
                return results
            logger.info(f"搜索 '{query}' {reason}，强制刷新缓存后重查...")
            fresh = self._do_fuzzy_search(query, max_results, force_refresh=True)
            # 取两次里更优的，避免刷新后反而变差
            if fresh and (not results or fresh[0].score > results[0].score):
                if results:
                    logger.info(f"刷新后重查更优: {fresh[0].score:.2f} > {results[0].score:.2f}")
                else:
                    logger.info(f"刷新后重查命中 {len(fresh)} 个结果")
                return fresh

        return results

    def fuzzy_search(
        self, query: str, max_results: int = 5, retry_on_empty: bool = True
    ) -> List[SearchResult]:
        """
        模糊搜索(支持自定义字段)

        参数:
            query: 搜索词
            max_results: 最大返回结果数
            retry_on_empty: 如果首次搜索无结果,是否自动清除缓存重试一次
        """
        logger.info(f"模糊搜索: '{query}'")
        return self.search_items(query, max_results, retry_on_empty)

    def _do_fuzzy_search(
        self, query: str, max_results: int = 5, force_refresh: bool = False
    ) -> List[SearchResult]:
        """执行实际的模糊搜索（内部方法，带索引加速）

        force_refresh=True 时阻塞重建缓存，用于"无结果 / 结果可疑"的重查。
        """
        # 确保缓存已装载（冷启动 / TTL 过期 / force 时同步刷新）。
        # 刻意不使用其返回值：空判定与取值统一走下方的原子快照，避免"判定用一代、
        # 取值用另一代"（R-3.5）。语义等价——无数据时二者同时为空。
        self._ensure_items(force=force_refresh)

        # 快速路径：索引精确/前缀匹配（索引随缓存一同构建）
        # 原子读取 (items, index)，避免与后台刷新交错时读到撕裂快照
        items, name_index = self._snapshot_items()
        if not items:
            return []
        norm_q = _normalize(query)
        exact_matches = name_index.get(norm_q, [])
        prefix_matches = []
        for name, name_items in name_index.items():
            if name.startswith(norm_q) and name != norm_q:
                prefix_matches.extend(name_items)
        indexed_items = exact_matches + prefix_matches

        # 如果索引命中足够，直接返回
        if len(indexed_items) >= max_results:
            results = []
            for item_dict in indexed_items[:max_results]:
                results.append(
                    SearchResult(
                        item=BwItem(
                            id=item_dict.get("id", ""),
                            name=item_dict.get("name", ""),
                            username=item_dict.get("login", {}).get("username", ""),
                            password="",
                            uris=[
                                u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])
                            ],
                            notes=item_dict.get("notes", ""),
                        ),
                        score=1.0,
                        matched_field="name",
                    )
                )
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
                    uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])],
                )

                scored.append(
                    SearchResult(item=bw_item, score=best_score, matched_field=matched_field)
                )

        # 将索引命中项也转为 SearchResult
        indexed_results = []
        for item_dict in indexed_items:
            indexed_results.append(
                SearchResult(
                    item=BwItem(
                        id=item_dict.get("id", ""),
                        name=item_dict.get("name", ""),
                        username=item_dict.get("login", {}).get("username", ""),
                        uris=[u.get("uri", "") for u in item_dict.get("login", {}).get("uris", [])],
                    ),
                    score=1.0,
                    matched_field="name",
                )
            )

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
                    logger.warning("密码为空,重试...")
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
            except BwTimeoutError as e:
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
    get_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})"
    )

    search_parser = subparsers.add_parser("search", help="搜索项目")
    search_parser.add_argument("query", help="搜索词")
    search_parser.add_argument("--session", help="BW_SESSION token")
    search_parser.add_argument("--limit", type=int, default=3, help="显示数量")
    search_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})"
    )

    list_parser = subparsers.add_parser("list", help="列出项目")
    list_parser.add_argument("--session", help="BW_SESSION token")
    list_parser.add_argument("--all", action="store_true", help="列出所有")
    list_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})"
    )

    test_parser = subparsers.add_parser("test", help="测试连接")
    test_parser.add_argument("--session", help="BW_SESSION token")
    test_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})"
    )

    ping_parser = subparsers.add_parser("ping", help="健康检查")
    ping_parser.add_argument("--session", help="BW_SESSION token")
    ping_parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"超时时间(秒,默认{DEFAULT_TIMEOUT})"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
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
    except BwTimeoutError as e:
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
