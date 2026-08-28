"""Request 到受限 RequestSummary 的确定性投影。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from harness_contracts import ErrorCode, Request, RequestError

from .models import RequestSummary


class RequestProjector(ABC):
    """应用可注入的请求脱敏与裁剪边界。"""

    @abstractmethod
    def project(self, request: Request) -> RequestSummary:
        """返回不包含可信身份、Tenant attributes 或 Trace baggage 的摘要。"""


class SafeRequestProjector(RequestProjector):
    """按 metadata allowlist 和 JSON 大小边界生成默认摘要。"""

    def __init__(
        self,
        *,
        metadata_allowlist: Iterable[str] = (),
        max_depth: int = 8,
        max_collection_items: int = 100,
        max_string_length: int = 4_096,
        max_total_values: int = 1_000,
    ) -> None:
        if isinstance(metadata_allowlist, str):
            raise TypeError("metadata_allowlist must be an iterable of strings")
        raw_allowlist = tuple(metadata_allowlist)
        if any(not isinstance(key, str) or not key.strip() for key in raw_allowlist):
            raise TypeError("metadata_allowlist entries must be non-empty strings")
        allowlist = frozenset(key.strip() for key in raw_allowlist)
        for name, value in (
            ("max_depth", max_depth),
            ("max_collection_items", max_collection_items),
            ("max_string_length", max_string_length),
            ("max_total_values", max_total_values),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

        self._metadata_allowlist = allowlist
        self._max_depth = max_depth
        self._max_collection_items = max_collection_items
        self._max_string_length = max_string_length
        self._max_total_values = max_total_values

    @property
    def metadata_allowlist(self) -> frozenset[str]:
        return self._metadata_allowlist

    def project(self, request: Request) -> RequestSummary:
        if not isinstance(request, Request):
            raise TypeError("request must be Request")

        metadata = {
            key: request.metadata[key]
            for key in sorted(self._metadata_allowlist)
            if key in request.metadata
        }
        counter = [0]
        self._validate_json(request.input.content, path="input.content", depth=0, counter=counter)
        self._validate_json(metadata, path="metadata", depth=0, counter=counter)

        return RequestSummary(
            request_id=request.request_id,
            input_type=request.input.type,
            input_content=_thaw_json(request.input.content),
            target_capability=(request.target.capability if request.target is not None else None),
            metadata=_thaw_json(metadata),
        )

    def _validate_json(
        self,
        value: Any,
        *,
        path: str,
        depth: int,
        counter: list[int],
    ) -> None:
        counter[0] += 1
        if counter[0] > self._max_total_values:
            self._raise_limit(
                "max_total_values",
                path=path,
                actual=counter[0],
                maximum=self._max_total_values,
            )
        if depth > self._max_depth:
            self._raise_limit(
                "max_depth",
                path=path,
                actual=depth,
                maximum=self._max_depth,
            )

        if isinstance(value, str):
            if len(value) > self._max_string_length:
                self._raise_limit(
                    "max_string_length",
                    path=path,
                    actual=len(value),
                    maximum=self._max_string_length,
                )
            return

        if isinstance(value, Mapping):
            if len(value) > self._max_collection_items:
                self._raise_limit(
                    "max_collection_items",
                    path=path,
                    actual=len(value),
                    maximum=self._max_collection_items,
                )
            for key, item in value.items():
                if len(key) > self._max_string_length:
                    self._raise_limit(
                        "max_string_length",
                        path=f"{path}.<key>",
                        actual=len(key),
                        maximum=self._max_string_length,
                    )
                self._validate_json(
                    item,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    counter=counter,
                )
            return

        if isinstance(value, tuple | list):
            if len(value) > self._max_collection_items:
                self._raise_limit(
                    "max_collection_items",
                    path=path,
                    actual=len(value),
                    maximum=self._max_collection_items,
                )
            for index, item in enumerate(value):
                self._validate_json(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    counter=counter,
                )

    @staticmethod
    def _raise_limit(limit: str, *, path: str, actual: int, maximum: int) -> None:
        raise RequestError(
            "request cannot be projected within routing summary limits",
            code=ErrorCode.REQUEST_INVALID,
            details={
                "reason": "request_summary_limit_exceeded",
                "limit": limit,
                "path": path,
                "actual": actual,
                "maximum": maximum,
            },
        )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_json(item) for item in value]
    return value
