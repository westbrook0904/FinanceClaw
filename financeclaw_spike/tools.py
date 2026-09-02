"""Minimal READ and WRITE BaseTool implementations used by the spike."""

import json
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr


class TransientReadError(ConnectionError):
    """Retryable error used to prove ToolRetryMiddleware behavior."""


class ReadMarketInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")


class WriteWatchlistInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    note: str = Field(default="", max_length=200)


class DemoReadTool(BaseTool):
    """Deterministic market-data READ with configurable transient failures."""

    name: str = "read_market_snapshot"
    description: str = "Read a deterministic demo market snapshot for one symbol."
    args_schema: type[BaseModel] = ReadMarketInput

    _remaining_failures: int = PrivateAttr(default=0)
    _call_count: int = PrivateAttr(default=0)

    def __init__(self, *, fail_first: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._remaining_failures = fail_first

    @property
    def call_count(self) -> int:
        return self._call_count

    def _run(self, symbol: str) -> str:
        self._call_count += 1
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise TransientReadError("demo upstream is temporarily unavailable")
        return json.dumps(
            {
                "symbol": symbol.upper(),
                "price": "100.00",
                "currency": "USD",
                "provider": "stage0-demo",
                "as_of": "2026-09-02T00:00:00Z",
            },
            sort_keys=True,
        )

    async def _arun(self, symbol: str) -> str:
        return self._run(symbol)


class DemoWriteTool(BaseTool):
    """In-memory WRITE whose execution must be guarded by HITL middleware."""

    name: str = "write_watchlist"
    description: str = "Add a symbol to the demo watchlist; this changes state."
    args_schema: type[BaseModel] = WriteWatchlistInput

    _writes: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @property
    def writes(self) -> tuple[dict[str, str], ...]:
        return tuple(self._writes)

    def _run(self, symbol: str, note: str = "") -> str:
        record = {"symbol": symbol.upper(), "note": note}
        self._writes.append(record)
        return json.dumps({"status": "written", **record}, sort_keys=True)

    async def _arun(self, symbol: str, note: str = "") -> str:
        return self._run(symbol, note)
