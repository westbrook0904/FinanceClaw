"""两个固定太卜 Tool：本地校验、治理、原文归档及有界投影。"""

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.mcp_client import TaibuMCPClient
from financeclaw.agent_server.tools.policy import ToolDecisionType, ToolPolicy
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.taibu import (
    TAIBU_TOOL_INPUTS,
    TaibuAlmanacInput,
    TaibuBaziInput,
    TaibuError,
    TaibuToolResult,
)
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.taibu import (
    TAIBU_SERVER_VERSION,
    contract_hash,
    remote_contracts,
    taibu_governance,
)


def input_error(error: ValidationError) -> str:
    """只公开缺项路径与本地约束说明，不回显出生参数或运行身份。"""
    issues = error.errors(include_input=False, include_url=False, include_context=False)
    fields = sorted(
        {".".join(str(p) for p in issue["loc"]) or "出生资料/日期口径" for issue in issues}
    )
    return "TAIBU_INPUT_INVALID: 请核对并澄清 " + ", ".join(fields) + "；不得猜测缺失值。"


class TaibuRuntimeInput(BaseModel):
    """运行身份由 ToolNode 注入，不能进入模型参数清单。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime[ExecutionContext]


class AlmanacRuntimeInput(TaibuAlmanacInput, TaibuRuntimeInput):
    """黄历输入与可信运行时。"""


class BaziRuntimeInput(TaibuBaziInput, TaibuRuntimeInput):
    """八字输入与可信运行时。"""


def remote_arguments(
    value: TaibuAlmanacInput | TaibuBaziInput, context: ExecutionContext
) -> tuple[dict, dict]:
    """确定性映射字段；时间只取可信请求锚点，出生校正只交给上游执行一次。"""
    if isinstance(value, TaibuAlmanacInput):
        resolved = value.date
        if value.day_offset is not None:
            try:
                clock = datetime.fromisoformat(context.request_clock or "")
                if clock.tzinfo is None:
                    raise ValueError("missing timezone")
                resolved = (
                    clock.astimezone(ZoneInfo(context.timezone)).date()
                    + timedelta(days=value.day_offset)
                ).isoformat()
            except (ValueError, OverflowError, ZoneInfoNotFoundError):
                raise TaibuError(
                    "TAIBU_CLOCK_REQUIRED", "缺少可信请求时钟或时区，请提供明确日期。"
                ) from None
        if not 1900 <= date.fromisoformat(resolved).year <= 2100:
            raise TaibuError("TAIBU_INPUT_INVALID", "查询日期必须在 1900–2100 年范围内。")
        args = {"date": resolved}
        if value.day_master is not None:
            args["dayMaster"] = value.day_master
        return args, {"date": resolved, "timezone": context.timezone}
    args = {
        "gender": value.gender,
        "calendarType": value.calendar_type,
        "isLeapMonth": value.is_leap_month or False,
        "detailLevel": "default",
        **{
            "birth" + part.capitalize(): getattr(value, "birth_" + part)
            for part in ("year", "month", "day", "hour", "minute")
        },
    }
    if value.longitude is not None:
        args["longitude"] = value.longitude
    return args, {
        "calendar_type": value.calendar_type,
        "is_leap_month": value.is_leap_month,
        "time_basis": value.time_basis,
        "solar_time": value.solar_time,
        "detail_level": "default",
    }


def validate_result(name: str, result, arguments: dict) -> tuple[dict, tuple[str, ...]]:
    """固定 JSON Schema 之外复验最少业务事实，绝不从 Markdown 猜 JSON。"""
    if result.isError:
        raise TaibuError("TAIBU_REMOTE_ERROR", "太卜执行失败，请核对历法及出生信息，原响应已归档。")
    data = result.structuredContent
    if not isinstance(data, dict) or not data:
        raise TaibuError("TAIBU_RESULT_INVALID", "太卜未返回非空结构化结果。")
    validator = Draft202012Validator(remote_contracts()[name]["outputSchema"])
    if next(validator.iter_errors(data), None) is not None:
        raise TaibuError("TAIBU_RESULT_INVALID", "太卜结果不符合已发布的输出 Schema。")
    warnings = []
    if name == "almanac":
        coordinate = data.get("基础与个性化坐标", {})
        activities = data.get("择日宜忌", {})
        if (
            not isinstance(coordinate, dict)
            or coordinate.get("日期") != arguments["date"]
            or not isinstance(activities, dict)
            or not all(
                isinstance(activities.get(key), list) and activities[key] for key in ("宜", "忌")
            )
        ):
            raise TaibuError("TAIBU_RESULT_INVALID", "黄历日期或核心宜忌结果不完整。")
        keys = ("基础与个性化坐标", "传统黄历基调", "择日宜忌", "神煞参考")
    else:
        pillars = data.get("四柱")
        if (
            not isinstance(pillars, list)
            or len(pillars) != 4
            or not all(
                isinstance(p, dict)
                and isinstance(p.get("干支"), str)
                and re.fullmatch(r"[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥]", p["干支"])
                for p in pillars
            )
            or {p.get("柱") for p in pillars} != {"年柱", "月柱", "日柱", "时柱"}
        ):
            raise TaibuError("TAIBU_RESULT_INVALID", "八字结果缺少完整四柱。")
        info = data.get("基本信息", {})
        if (
            not isinstance(info, dict)
            or info.get("性别") != {"male": "男", "female": "女"}[arguments["gender"]]
        ):
            raise TaibuError("TAIBU_RESULT_INVALID", "八字结果与请求主体参数不符。")
        location = data.get("placeResolutionInfo", {})
        if "longitude" in arguments:
            if (
                not isinstance(location, dict)
                or location.get("resolved") is not True
                or location.get("source") != "manual_input"
                or location.get("usedLongitude") != arguments["longitude"]
            ):
                raise TaibuError("TAIBU_CONVENTION_UNRESOLVED", "所要求的真太阳时地点处理未完成。")
        elif isinstance(location, dict) and location.get("resolved") is not True:
            warnings.append("按明确的中国标准时间排盘，未进行出生地或真太阳时校正。")
        warnings.append("首期返回默认级别八字事实；术数解读不作为金融事实或已验证预测。")
        keys = ("基本信息", "四柱", "干支关系", "placeResolutionInfo")
    return {key: data[key] for key in keys if key in data}, tuple(warnings)


class TaibuTool(BaseTool):
    """原始返回存受保护工件，ToolMessage 只携带可用投影与来源引用。"""

    args_schema: type[BaseModel]
    client: Any = Field(exclude=True, repr=False)
    artifacts: Any = Field(exclude=True, repr=False)
    governance: Any = Field(exclude=True, repr=False)
    projection_bytes: int
    handle_tool_error: bool = True
    handle_validation_error: Any = input_error

    @property
    def tool_call_schema(self) -> type[BaseModel]:
        """只把本地固定契约给模型，排除运行时和连接配置。"""
        return TAIBU_TOOL_INPUTS[self.name]

    def _run(self, **arguments):
        """同步调用使用独立事件循环，不共享活动 HTTP session。"""
        return asyncio.run(self._arun(**arguments))

    async def _arun(self, *, runtime: ToolRuntime[ExecutionContext], **arguments) -> ToolMessage:
        """复验上下文授权，完成调用、归档和投影，并保持错误状态可审计。"""
        context = ExecutionContext.model_validate(runtime.context)
        if not runtime.tool_call_id:
            raise ToolException("TAIBU_CONTEXT_REQUIRED: 缺少可信工具调用身份。")
        decision = ToolPolicy().evaluate(context, self.governance, arguments)
        if decision.effect is not ToolDecisionType.ALLOW:
            raise ToolException("TAIBU_ACCESS_DENIED: 当前身份不允许使用此太卜工具。")
        try:
            value = TAIBU_TOOL_INPUTS[self.name].model_validate(arguments)
            args, convention = remote_arguments(value, context)
            name = self.name.removeprefix("taibu_")
            called_at = datetime.now(UTC).isoformat()
            started = perf_counter()
            raw = await self.client.call(name, args)
            elapsed_ms = round((perf_counter() - started) * 1000, 3)
        except TaibuError as error:
            raise ToolException(str(error)) from None
        snapshot = {
            "tool": self.name,
            "remote_tool": name,
            "provider": "taibu",
            "server_version": TAIBU_SERVER_VERSION,
            "contract_hash": contract_hash(remote_contracts()[name]),
            "called_at": called_at,
            "elapsed_ms": elapsed_ms,
            "arguments": args,
            "convention": convention,
            "response": raw.model_dump(mode="json", exclude_none=True),
        }
        payload_hash = sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        saved = await asyncio.to_thread(
            self.artifacts.persist,
            snapshot,
            context=context,
            source_type="taibu_result",
            source_id=runtime.tool_call_id,
            idempotency_key=f"{context.turn_id}:{runtime.tool_call_id}:{payload_hash}",
        )
        reference = {
            "artifact_id": saved.artifact_id,
            "content_hash": saved.content_hash,
            "size_bytes": saved.size_bytes,
            "source_turn_id": context.turn_id,
        }
        data, warnings, failure = {}, (), None
        try:
            data, warnings = validate_result(name, raw, args)
        except TaibuError as error:
            failure = str(error)
        result = TaibuToolResult(
            outcome="error" if failure else "success",
            tool=self.name,
            remote_tool=name,
            server_version=TAIBU_SERVER_VERSION,
            contract_hash=snapshot["contract_hash"],
            called_at=called_at,
            elapsed_ms=elapsed_ms,
            convention=convention,
            data=data,
            warnings=warnings,
            error=failure,
            artifact_ref=reference,
        )
        content = result.model_dump_json()
        message_bytes = len(
            json.dumps({"content": content, "artifact": reference}, ensure_ascii=False).encode()
        )
        if (
            len(content.encode()) > self.projection_bytes
            or message_bytes > self.artifacts.inline_bytes
        ):
            result = result.model_copy(
                update={
                    "outcome": "error",
                    "data": {},
                    "error": "TAIBU_PROJECTION_TOO_LARGE: 结果超过投影预算，请按引用回读原始工件。",
                }
            )
            content = result.model_dump_json()
        return ToolMessage(
            content=content,
            artifact=reference,
            name=self.name,
            tool_call_id=runtime.tool_call_id,
            status="error" if result.outcome == "error" else "success",
        )


def taibu_tools(
    settings: FinanceClawSettings, artifacts: ArtifactService | None, *, client=None
) -> tuple[ManagedTool, ...]:
    """只在启用且可持久归档时装配，构造阶段不连接远端。"""
    if not settings.taibu_enabled:
        return ()
    if artifacts is None:
        raise ValueError("Taibu tools require persistent ArtifactService")
    if artifacts.inline_bytes < 4096:
        raise ValueError("Taibu ArtifactService must support at least 4096 inline bytes")
    client = client or TaibuMCPClient(settings)
    descriptions = {
        "taibu_almanac": (
            "查询太卜黄历。date 或 day_offset 二选一；今天 day_offset=0，"
            "以本轮时钟为准。未知日期先澄清。"
        ),
        "taibu_bazi": (
            "计算太卜八字。确认性别、历法、出生日期/时分和中国标准时间口径。"
            "solar_time 决定是否按经度校正一次；不得猜测未知资料或输入已校正时间。"
        ),
    }
    schemas = {"taibu_almanac": AlmanacRuntimeInput, "taibu_bazi": BaziRuntimeInput}
    return tuple(
        ManagedTool(
            TaibuTool(
                name=governance.tool_id,
                description=descriptions[governance.tool_id],
                args_schema=schemas[governance.tool_id],
                client=client,
                artifacts=artifacts,
                governance=governance,
                projection_bytes=min(
                    settings.taibu_projection_bytes, artifacts.inline_bytes - 1024
                ),
            ),
            governance,
        )
        for governance in taibu_governance(settings)
    )
