"""
数据类型模块

负责:
- BwItem / SearchResult 数据类
- BwTimeoutError / LockedError / BwConnectionError 异常类

命名约定: 自定义异常一律带 `Bw` 前缀，**不要**遮蔽内置的 `TimeoutError` /
`ConnectionError` —— 否则 `except ConnectionError` 会静默漏捕内置 OSError 子类
（如 socket 相关错误），且类型语义会随导入路径而变。
"""
from dataclasses import dataclass
from typing import List, Optional

# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class BwItem:
    """Bitwarden 项目"""
    id: str
    name: str
    username: str = ""
    password: str = ""
    uris: Optional[List[str]] = None
    notes: str = ""

    def __post_init__(self):
        if self.uris is None:
            self.uris = []


@dataclass
class SearchResult:
    """搜索结果"""
    item: BwItem
    score: float
    matched_field: str


# ============================================================================
# 异常定义
# ============================================================================

class BwTimeoutError(Exception):
    """请求超时异常（区别于内置 TimeoutError）"""
    pass


class LockedError(Exception):
    """金库已锁定异常"""
    pass


class BwConnectionError(Exception):
    """MCP/守护进程连接异常(进程无法启动/通信失败)"""
    pass
