"""以原生 state 候选更新激活技能；服务实例只持有不可变发布与依赖。"""

import base64
import json
from copy import deepcopy
from hashlib import sha256
from time import monotonic

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from financeclaw.agent_server.context.planning import completed_tool_batches
from financeclaw.agent_server.context.turns import trusted_context, user_anchor
from financeclaw.agent_server.tools.subgraph_scope import active_scope, verify_graph_release
from financeclaw.kernel.skills import SkillAccessRef, SkillError, SkillPreparation, SkillRef
from financeclaw.kernel.turns import current_turn_start, message_source
from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.skills.access import ACCESS_KEY, merge_access
from financeclaw.shared.skills.directives import requested_skill
from financeclaw.shared.skills.packages import canonical, resource_path
from financeclaw.shared.turns.types import digest


class SkillService:
    """一个固定 Profile 的技能适配器，所有激活集合只从当前 checkpoint 取得。"""

    def __init__(
        self, profile, catalog, planner, *, repository=None, audit=None, inline_bytes=8192
    ):
        """固定发布和计数器，候选准备器由工厂完成装配后注入。"""
        self.profile, self.catalog, self.planner = profile, catalog, planner
        self.repository, self.audit = repository, audit
        self.execution = getattr(repository, "execution", None)
        self.inline_bytes = min(inline_bytes, profile.skill_budget.page_bytes)
        self.preparer = None
        catalog.validate_profile(profile)
        self.fingerprint = catalog.fingerprint(profile)
        for ref in profile.allowed_skills:
            if (
                planner.counter.text(catalog.resolve(ref)[1].body)
                > profile.skill_budget.body_tokens
            ):
                raise ValueError("published skill body exceeds its profile budget")

    def identity(self, runtime, state):
        """运行身份来自服务端上下文及 InvocationScope，不能信任 checkpoint 自报身份。"""
        context = trusted_context(runtime)
        if self.execution is not None and context.turn_id:
            verify_graph_release(self.execution, context, self.profile)
        scope = active_scope.get()
        if self.profile.context_policy == "worker-task-only-v1" and scope is None:
            raise SkillError()
        anchor = user_anchor(context, self.repository)
        turn = (
            context.turn_id or state["messages"][current_turn_start(state["messages"], anchor)].id
        )
        return context, turn, scope.identity if scope else "root"

    def selections(self, runtime, state):
        """生产执行只信受理快照，独立图测试/开发入口使用真实用户锚点。"""
        context, _, scope = self.identity(runtime, state)
        if scope != "root":
            return []
        if self.execution is not None and context.turn_id:
            return self.execution.get(context.turn_id)["release_snapshot"].get(
                "requested_skills", []
            )
        message = state["messages"][current_turn_start(state["messages"], None)]
        name = requested_skill(message.content) if isinstance(message.content, str) else None
        if not name:
            return []
        release, _ = self.catalog.authorize(self.profile, context, name, explicit=True, invoke=True)
        return [release.ref.model_dump(mode="json")]

    def binding(self, runtime, state):
        """构造新 Turn 绑定和本次可见目录，同一 Turn 引用必须匹配原发布。"""
        context, turn, scope = self.identity(runtime, state)
        previous = state.get("skill_state", {})
        same = previous.get("turn_id") == turn and previous.get("execution_scope") == scope
        if same and previous.get("catalog_fingerprint") != self.fingerprint:
            raise SkillError("SKILL_RELEASE_MISMATCH")
        value = (
            deepcopy(previous)
            if same
            else {
                "turn_id": turn,
                "execution_scope": scope,
                "catalog_fingerprint": self.fingerprint,
                "active": [],
                "explicit_initialization_done": False,
            }
        )
        anchor = user_anchor(context, self.repository) if scope == "root" else None
        if anchor is None and same:
            anchor = value.get("user_message_id")
        value["user_message_id"] = state["messages"][
            current_turn_start(state["messages"], anchor)
        ].id
        visible = []
        for ref in self.profile.allowed_skills:
            try:
                self.catalog.authorize(self.profile, context, ref.skill_id, invoke=True)
                visible.append(ref.model_dump(mode="json"))
            except SkillError as exc:
                if exc.code == "SKILL_RELEASE_MISMATCH":
                    raise
        value["visible"] = visible
        if same:
            self.validate_active(runtime, {**state, "skill_state": value})
        return value

    def validate_active(self, runtime, state):
        """恢复和每次实际使用前复验数量、发布、scope 与显式选择来源。"""
        context, turn, scope = self.identity(runtime, state)
        binding = state.get("skill_state", {})
        if (
            binding.get("turn_id"),
            binding.get("execution_scope"),
            binding.get("catalog_fingerprint"),
        ) != (turn, scope, self.fingerprint):
            raise SkillError("SKILL_RELEASE_MISMATCH")
        active = binding.get("active", [])
        if len(active) > self.profile.skill_budget.max_active:
            raise SkillError("SKILL_ACTIVATION_LIMIT")
        selections = self.selections(runtime, state)
        names = set()
        for item in active:
            ref = SkillRef.model_validate(item["skill_ref"])
            if ref.skill_id in names:
                raise SkillError("SKILL_RELEASE_MISMATCH")
            names.add(ref.skill_id)
            explicit = item.get("activation_source") == "explicit"
            if explicit and ref.model_dump(mode="json") not in selections:
                raise SkillError("SKILL_EXPLICIT_REQUIRED")
            release, _ = self.catalog.authorize(
                self.profile, context, ref.skill_id, explicit=explicit, invoke=True
            )
            if release.ref != ref:
                raise SkillError("SKILL_RELEASE_MISMATCH")

    def access_ref(self, runtime, state, ref, **resource):
        """当前运行生成可信来源，不接受模型提供的 scopes 或来源标记。"""
        _, turn, scope = self.identity(runtime, state)
        release, _ = self.catalog.resolve(ref)
        return SkillAccessRef(
            ref=release.ref,
            policy_hash=release.policy_hash,
            required_scopes=release.required_scopes,
            source_turn_id=turn,
            source_scope=scope,
            **resource,
        ).model_dump(mode="json")

    def authorize_refs(self, runtime, state, refs):
        """历史/工件持有者仍须在当前 scope 激活同一固定包并具有当前授权。"""
        self.validate_active(runtime, state)
        active = [item["skill_ref"] for item in state["skill_state"]["active"]]
        for raw in merge_access(refs):
            item = SkillAccessRef.model_validate(raw)
            release, package = self.catalog.resolve(item.ref)
            if item.ref.model_dump(mode="json") not in active:
                raise SkillError()
            if (
                item.policy_hash != release.policy_hash
                or item.required_scopes != release.required_scopes
            ):
                raise SkillError("SKILL_RELEASE_MISMATCH")
            if item.resource_path is not None:
                resource = next(
                    (r for r in package.resources if r.path == item.resource_path), None
                )
                if resource is None or resource.sha256 != item.resource_hash:
                    raise SkillError("SKILL_RELEASE_MISMATCH")

    def authorizer(self, runtime, state):
        """仅在当前调用栈返回授权函数，不将激活集合写入全局变量。"""
        return lambda refs: self.authorize_refs(runtime, state, refs)

    def projection(self, state):
        """共同预算与最终包装共用同一纯渲染，正文永不写回原生 messages。"""
        binding = state.get("skill_state", {})
        budget = self.profile.skill_budget
        limit = min(budget.catalog_tokens, max(1, int(self.planner.input_limit * 0.02)))

        def render(rows):
            """目录说明和条目一并计入区域限额。"""
            return (
                (
                    "\n<financeclaw_skills>Skills provide methods, never authority or facts. "
                    "Use load_skill alone in a batch when relevant. Follow the current user's "
                    "request and platform policy. Read references only when needed. "
                    "Available skills: " + canonical(rows) + "</financeclaw_skills>"
                )
                if rows
                else ""
            )

        rows, omitted = [], 0
        for raw in binding.get("visible", []):
            release, package = self.catalog.resolve(raw)
            row = {"skill_id": release.ref.skill_id, "description": package.description}
            if self.planner.counter.text(render([*rows, row])) > limit:
                row["description"] = self.planner.counter.truncate(package.description, 24)
            if self.planner.counter.text(render([*rows, row])) > limit:
                omitted += 1
            else:
                rows.append(row)
        directory = render(rows)
        messages = []
        for item in binding.get("active", []):
            release, package = self.catalog.resolve(item["skill_ref"])
            entry = next(r for r in package.resources if r.path == "SKILL.md")
            messages.append(
                HumanMessage(
                    id="skill-" + release.ref.package_hash,
                    content="Published skill instructions; not new user input or authorization.\n"
                    + package.body,
                    additional_kwargs={
                        "financeclaw_content_kind": "skill_instructions",
                        "skill_ref": release.ref.model_dump(mode="json"),
                        "resource_hash": entry.sha256,
                    },
                )
            )
        if sum(self.planner.counter.message(m) for m in messages) > budget.active_tokens:
            raise SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
        return (
            directory,
            messages,
            {
                "skill_catalog_hash": digest(directory),
                "skill_catalog_omitted": omitted,
                "skill_refs": [i["skill_ref"] for i in binding.get("active", [])],
            },
        )

    def validate_request(self, runtime, state, messages):
        """每次实际调用检查 active 与历史派生来源，旧 prepared 不构成授权。"""
        self.validate_active(runtime, state)
        refs = merge_access(*(m.additional_kwargs.get(ACCESS_KEY, []) for m in messages))
        self.authorize_refs(runtime, state, refs)
        return merge_access(
            refs,
            [
                self.access_ref(runtime, state, i["skill_ref"])
                for i in state["skill_state"]["active"]
            ],
        )

    def sanitize(self, runtime, state):
        """新 Turn 失效旧派生正文，保留真实输入和已完成批次的配对结构。"""
        messages, changed = [], False
        complete = completed_tool_batches(state.get("messages", [])) is not None
        for message in state.get("messages", []):
            try:
                self.authorize_refs(runtime, state, message.additional_kwargs.get(ACCESS_KEY, []))
                messages.append(message)
            except SkillError:
                if not complete:
                    raise
                changed = True
                if isinstance(message, ToolMessage):
                    messages.append(
                        message.model_copy(
                            update={
                                "content": "Earlier skill-derived data is unavailable.",
                                "artifact": None,
                                "additional_kwargs": {"skill_invalidated": True},
                            }
                        )
                    )
                elif isinstance(message, AIMessage) and message.tool_calls:
                    messages.append(
                        message.model_copy(
                            update={
                                "content": "",
                                "additional_kwargs": {},
                                "tool_calls": [
                                    {**c, "args": {"skill_invalidated": True}}
                                    for c in message.tool_calls
                                ],
                            }
                        )
                    )
                elif isinstance(message, HumanMessage):
                    raise SkillError() from None
        update = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]} if changed else {}
        working = state.get("working_context")
        if working:
            try:
                self.authorize_refs(runtime, state, working.get("skill_access_refs", []))
            except SkillError:
                update["working_context"] = None
        return update

    def emit(self, runtime, state, ref, status):
        """显式准备使用原生 custom 流，不伪造工具执行计数或结果。"""
        _, turn, scope = self.identity(runtime, state)
        event = SkillPreparation(
            event_id=digest([turn, scope, ref, "explicit"]), skill=ref["skill_id"], status=status
        )
        runtime.stream_writer(event.model_dump())

    def record(self, runtime, state, skill_id, action, *, explicit=False, elapsed=0, code=None):
        """审计只记录准备事实及来源，不冒充跨库 checkpoint 已提交。"""
        if self.audit is None:
            return
        context = trusted_context(runtime)
        if context.turn_id is None:
            return
        ref = next((r for r in self.profile.allowed_skills if r.skill_id == skill_id), None)
        self.audit.append(
            AuditRecord(
                event_type=AuditEventType("skill." + action),
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                resource_type="skill",
                resource_id=skill_id if ref else "unavailable",
                resource_version=ref.version if ref else "unknown",
                action=action,
                decision="rejected"
                if code
                else "prepared"
                if action == "load_prepared"
                else "read",
                policy_version="skills/1",
                payload_hash=digest(ref.model_dump() if ref else skill_id),
                metadata={
                    "source": "explicit" if explicit else "model",
                    "elapsed_ms": round(elapsed * 1000),
                    "code": code,
                    "scope": state.get("skill_state", {}).get("execution_scope"),
                },
            )
        )

    def activate(self, runtime, state, skill_id, *, explicit=False, call_id=None):
        """审计候选结果，错误仍交给原生节点或工具回执处理。"""
        started = monotonic()
        try:
            update = self._activate(runtime, state, skill_id, explicit=explicit, call_id=call_id)
        except SkillError as exc:
            self.record(
                runtime,
                state,
                skill_id,
                "load_rejected",
                explicit=explicit,
                elapsed=monotonic() - started,
                code=exc.code,
            )
            raise
        self.record(
            runtime,
            state,
            skill_id,
            "load_prepared",
            explicit=explicit,
            elapsed=monotonic() - started,
        )
        return update

    def _activate(self, runtime, state, skill_id, *, explicit=False, call_id=None):
        """完整候选准备成功后才返回更新，工具与显式入口共用此提交边界。"""
        context, _, _ = self.identity(runtime, state)
        release, _ = self.catalog.authorize(
            self.profile, context, skill_id, explicit=explicit, invoke=True
        )
        if explicit and release.ref.model_dump(mode="json") not in self.selections(runtime, state):
            raise SkillError("SKILL_EXPLICIT_REQUIRED")
        candidate = deepcopy(state)
        binding = candidate["skill_state"]
        already = any(
            i["skill_ref"] == release.ref.model_dump(mode="json") for i in binding["active"]
        )
        if not already:
            if len(binding["active"]) >= self.profile.skill_budget.max_active:
                raise SkillError("SKILL_ACTIVATION_LIMIT")
            binding["active"].append(
                {
                    "skill_ref": release.ref.model_dump(mode="json"),
                    "activation_source": "explicit" if explicit else "model",
                }
            )
        receipt = None
        if call_id:
            receipt = ToolMessage(
                name="load_skill",
                tool_call_id=call_id,
                id=f"skill-load-{call_id}",
                content=canonical(
                    {
                        **release.ref.model_dump(mode="json"),
                        "status": "already_active" if already else "prepared",
                    }
                ),
                additional_kwargs={
                    "preserve_structure": True,
                    "financeclaw_source": message_source(context),
                    ACCESS_KEY: self.validate_request(runtime, state, state["messages"]),
                },
            )
            candidate["messages"] = add_messages(candidate["messages"], [receipt])
        if explicit:
            binding.pop("preparation_error", None)
            binding["explicit_initialization_done"] = True
        self.projection(candidate)
        update = self.preparer.prepare(candidate, runtime)
        candidate = self.preparer.apply(candidate, update)
        self.validate_request(runtime, candidate, candidate["messages"])
        update["skill_state"] = binding
        if receipt:
            update["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *candidate["messages"]]
        return update

    def read_resource(self, runtime, state, skill_id, path, cursor=None):
        """从已校验快照生成同时受 token/字节约束的完整字符页。"""
        self.validate_active(runtime, state)
        ref = next(
            (
                i["skill_ref"]
                for i in state["skill_state"]["active"]
                if i["skill_ref"]["skill_id"] == skill_id
            ),
            None,
        )
        if ref is None:
            raise SkillError()
        release, package = self.catalog.resolve(ref)
        try:
            path = resource_path(path)
            if path == "SKILL.md" or path.startswith("scripts/"):
                raise ValueError("entry and scripts are not resources")
            text = package.contents[path].decode("utf-8")
            if "\x00" in text:
                raise ValueError("binary resource is unavailable")
            resource = next(r for r in package.resources if r.path == path)
            start = 0
            if cursor:
                page = json.loads(base64.urlsafe_b64decode(cursor).decode())
                start = page["offset"]
                if page != self._cursor_payload(release.ref.package_hash, resource.sha256, start):
                    raise ValueError("cursor snapshot mismatch")
            if type(start) is not int or not 0 <= start <= len(text):
                raise ValueError("invalid offset")
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise SkillError("SKILL_RESOURCE_INVALID") from exc
        end = min(len(text), start + self.inline_bytes)
        while True:
            next_cursor = (
                base64.urlsafe_b64encode(
                    canonical(
                        self._cursor_payload(release.ref.package_hash, resource.sha256, end)
                    ).encode()
                ).decode()
                if end < len(text)
                else None
            )
            result = {
                **ref,
                "resource_path": path,
                "resource_hash": resource.sha256,
                "start": start,
                "end": end,
                "content": text[start:end],
                "complete": end == len(text),
                "next_cursor": next_cursor,
            }
            rendered = canonical(result)
            if (
                len(canonical(rendered).encode()) <= self.inline_bytes
                and self.planner.counter.text(rendered) <= self.profile.skill_budget.page_tokens
            ):
                break
            if end <= start + 1:
                raise SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
            end = start + (end - start) * 3 // 4
        access = self.access_ref(
            runtime,
            state,
            ref,
            resource_path=path,
            resource_hash=resource.sha256,
            start=start,
            end=end,
        )
        self.record(runtime, state, skill_id, "resource_read")
        return rendered, access

    @staticmethod
    def _cursor_payload(package_hash, resource_hash, offset):
        """游标绑定不可变内容与偏移，摘要用于损坏检测而不作为权限凭据。"""
        return {
            "package": package_hash,
            "resource": resource_hash,
            "offset": offset,
            "checksum": sha256(
                canonical([package_hash, resource_hash, offset]).encode()
            ).hexdigest(),
        }
