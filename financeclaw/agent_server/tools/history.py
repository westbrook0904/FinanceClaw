"""受控历史与工件回读；所有工具共用身份和分页边界。"""

from typing import Annotated, Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from pydantic import Field, PrivateAttr

from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.memory import MemoryToolInput
from financeclaw.kernel.skills import SkillError
from financeclaw.shared.artifacts.views import READ_KEY, REFERENCE_BYTES, encode
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

    artifact_id: str = Field(
        description="Copy the artifact_id from an existing tool/history result"
    )
    content_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="Copy the content_hash paired with that artifact_id"
    )
    mode: Literal["inspect", "json", "text"]
    path: str = Field(
        default="",
        max_length=1024,
        description=(
            "JSON Pointer from the business-data root, not the archive wrapper. "
            "Use an empty string for the root: path='' reads a hotel-detail object itself. "
            "Example: path='/hotelInformationList' selects the hotel array inside a result. "
            "Every non-empty path must start with '/'; '$', dot notation and wildcards are "
            "not supported. '/' selects an empty-name key, not the root. "
            "Within a key, escape '/' as '~1' and '~' as '~0'."
        ),
    )
    fields: list[Annotated[str, Field(max_length=1024)]] = Field(
        default_factory=list,
        max_length=24,
        description=(
            "JSON Pointer field paths within the selected object or each selected array record. "
            "Every entry must start with '/': use ['/name', '/price'], not ['name', 'price']. "
            "Do not repeat path here: with path='/hotelInformationList', use fields=['/name']. "
            "An empty list selects all fields; missing fields are reported in missing_fields."
        ),
    )
    start: int = Field(default=0, ge=0, description="Array record offset; ignored for objects")
    limit: int = Field(
        default=20, ge=1, le=200, description="Maximum array records; ignored for objects"
    )
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=8000)


class HistoryTool(BaseTool):
    """薄适配器，检索和归属规则由 HistoryService 统一实现。"""

    _service: Any = PrivateAttr()
    _skills: Any = PrivateAttr(default=None)
    handle_tool_error: bool = True

    def __init__(self, service, **kwargs):
        """保存历史原文及工件读取所需的服务。"""
        super().__init__(**kwargs)
        self._service = service

    def _run(self, *, runtime, **arguments):
        """用可信身份执行明确的历史检索或分页回读。"""
        context = trusted_context(runtime)
        authorizer = self._skills.authorizer(runtime, runtime.state) if self._skills else None
        try:
            epoch = self._service.privacy_epoch(context)
            if self.name == "search_history":
                result = self._service.search(
                    context, runtime.store, skill_authorizer=authorizer, **arguments
                )
            elif self.name == "read_history":
                result = self._service.read(context, skill_authorizer=authorizer, **arguments)
            else:
                result = self._service.read_artifact(
                    context, skill_authorizer=authorizer, **arguments
                )
            provenance = self._service.result_provenance(
                context,
                result,
                tool_name=self.name,
                initial_epoch=epoch,
                skill_authorizer=authorizer,
            )
        except SkillError as exc:
            return ToolMessage(
                content=encode(exc.payload()),
                status="error",
                name=self.name,
                tool_call_id=runtime.tool_call_id,
            )
        except (ValueError, LookupError, PermissionError) as exc:
            raise ToolException(str(exc)) from exc
        if self.name == "read_artifact":
            reference = {
                key: result[key]
                for key in ("artifact_id", "content_hash", "source_turn_id", "size_bytes", "format")
            }
            reference["read_with"] = "read_artifact"
            query = {
                key: value
                for key, value in arguments.items()
                if key not in {"artifact_id", "content_hash"}
            }
            if len(encode({**reference, "last_read": query}).encode()) < REFERENCE_BYTES - 64:
                reference["last_read"] = query
            provenance.update(artifact_ref=reference, **{READ_KEY: query})
        return ToolMessage(
            name=self.name,
            tool_call_id=runtime.tool_call_id,
            content=encode(result),
            additional_kwargs=provenance,
        )


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
            "Read a saved result by ID and hash without rerunning its source tool. "
            "Use mode=inspect for field/array paths; mode=json with path, fields, start and limit "
            "for complete records (follow next_start); mode=text for character pages. "
            "JSON paths are relative to business data, not the archive wrapper. "
            "path='' selects the root; path='/hotelInformationList' selects a hotel array. "
            "Every fields entry is also a JSON Pointer: fields=['/name', '/price'], "
            "never bare field names. "
            "Select useful fields to read many records efficiently. "
            "For a single object, use path and fields; start/limit apply only to arrays. "
            "Copy a real ID and its matching hash from a prior result; never invent references "
            "or use placeholders to probe this tool. If no reference exists, query the source "
            "tool or search/read history first. "
            "A preview/page is not all results.",
        ),
    )
    return tuple(
        ManagedTool(
            HistoryTool(
                service,
                name=name,
                description=description,
                args_schema=schema,
                metadata={"artifact_reader": name == "read_artifact", "skill_provenance": True},
            ),
            governance,
        )
        for (name, schema, description), governance in zip(
            definitions, history_tool_governance(), strict=True
        )
    )
