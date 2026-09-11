"""受控历史与工件回读；所有工具共用身份和分页边界。"""

import json
from typing import Any

from langchain_core.tools import BaseTool, ToolException
from pydantic import Field, PrivateAttr

from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.memory import MemoryToolInput
from financeclaw.shared.releases.tools import history_tool_governance


class SearchHistoryInput(MemoryToolInput):
    """默认当前会话；跨会话范围必须显式请求。"""

    query: str = Field(min_length=1, max_length=512)
    conversation_id: str | None = None
    across_conversations: bool = False
    limit: int = Field(default=6, ge=1, le=20)


class ReadHistoryInput(MemoryToolInput):
    """以 Turn 和字符偏移回读问答。"""

    turn_id: str
    artifact_offset: int = Field(default=0, ge=0)
    conversation_id: str | None = None
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=8000)


class ReadArtifactInput(MemoryToolInput):
    """工件引用必须包含内容 hash，不接收任意路径或 URL。"""

    artifact_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=8000)


class HistoryTool(BaseTool):
    """薄适配器，检索和归属规则由 HistoryService 统一实现。"""

    _service: Any = PrivateAttr()
    handle_tool_error: bool = True

    def __init__(self, service, **kwargs):
        """保存历史原文及工件读取所需的服务。"""
        super().__init__(**kwargs)
        self._service = service

    def _run(self, *, runtime, **arguments):
        """用可信身份执行明确的历史检索或分页回读。"""
        context = trusted_context(runtime)
        try:
            if self.name == "search_history":
                result = self._service.search(context, runtime.store, **arguments)
            elif self.name == "read_history":
                result = self._service.read(context, **arguments)
            else:
                result = self._service.read_artifact(context, **arguments)
        except (ValueError, LookupError, PermissionError) as exc:
            raise ToolException(str(exc)) from exc
        return json.dumps(result, ensure_ascii=False)


def history_tools(service) -> tuple[ManagedTool, ...]:
    """三个明确的公开入口，不提供任意存储读取能力。"""
    definitions = (
        (
            "search_history",
            SearchHistoryInput,
            "Search past completed conversations by meaning. Results are historical; "
            "read their source Turn for details.",
        ),
        (
            "read_history",
            ReadHistoryInput,
            "Read a page of an owned Turn's original conversation and its "
            "archived tool-result directory.",
        ),
        (
            "read_artifact",
            ReadArtifactInput,
            "Read a page of an archived tool result using its ID and hash. "
            "This reads the saved snapshot without rerunning the tool.",
        ),
    )
    return tuple(
        ManagedTool(
            HistoryTool(service, name=name, description=description, args_schema=schema), governance
        )
        for (name, schema, description), governance in zip(
            definitions, history_tool_governance(), strict=True
        )
    )
