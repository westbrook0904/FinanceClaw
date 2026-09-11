"""区分业务检索用途和供应商 embedding 方法，不根据文本猜测计费用途。"""

import logging
from contextlib import contextmanager
from time import monotonic

LOGGER = logging.getLogger(__name__)


@contextmanager
def store_operation(purpose: str, *, query_chars: int = 0, index_items: int = 0):
    """记录逻辑 Store 操作；实际 embedding 方法调用由 Embeddings 适配器另行计量。"""
    started = monotonic()
    success = False
    try:
        yield
        success = True
    finally:
        LOGGER.info(
            "memory_store purpose=%s query_chars=%d index_items=%d seconds=%.3f success=%s",
            purpose,
            query_chars,
            index_items,
            monotonic() - started,
            success,
        )
