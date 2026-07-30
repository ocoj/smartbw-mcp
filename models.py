"""
数据类型模块

负责:
- BwItem / SearchResult 数据类
- TimeoutError / LockedError / ConnectionError 异常类
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

class TimeoutError(Exception):
    """请求超时异常"""
    pass


class LockedError(Exception):
    """金库已锁定异常"""
    pass


class ConnectionError(Exception):
    """MCP 连接异常(进程无法启动/通信失败)"""
    pass
