"""发布配置的确定性指纹：冻结模型参数、降级链与具体工具治理绑定。"""

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from pydantic import BaseModel


def configuration_fingerprint(*values: Any) -> str:
    """只处理声明配置，绝不传入 ModelFactory 或包含密钥的 Settings 对象。"""
    encoded = json.dumps(
        _canonical(values), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return sha256(encoded.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    """保留顺序敏感的列表，只排序集合，避免跨进程哈希随机化造成误冲突。"""
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(
            (_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
        )
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    return value
