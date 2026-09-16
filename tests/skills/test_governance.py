"""候选提交、资料分页、工件及 Journal 来源约束。"""

import base64
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from financeclaw.agent_server.context.artifacts import ToolResultArchive
from financeclaw.agent_server.context.planning import history_messages, projected_messages
from financeclaw.agent_server.context.preparation import CandidatePreparation
from financeclaw.agent_server.memory.history import HistoryService
from financeclaw.agent_server.middleware.final_context import RequestRecorder
from financeclaw.agent_server.skills.service import SkillService
from financeclaw.kernel.skills import SkillError
from financeclaw.shared.releases.skills import builtin_skills
from financeclaw.shared.skills.access import ACCESS_KEY, RESOURCE_KEY
from financeclaw.shared.skills.catalog import SkillCatalog
from financeclaw.shared.skills.packages import load_package
from tests.skills.test_packages import package
from tests.stage1.test_agent import components_with_tools, context
from tests.stage11.test_context_evidence import stack as evidence_stack
from tests.stage11.test_context_native import BoundFakeModel, budget

stack = evidence_stack


def service_state(*, ctx=None, profile_changes=None, catalog=None, user_message_id="user"):
    """单独绑定行情技能以检验其权限边界，不依赖全局内置包数量和排序。"""
    components, _ = components_with_tools()
    if catalog is None:
        catalog = SkillCatalog(
            entry
            for entry in builtin_skills().entries.values()
            if entry[0].ref.skill_id == "market-brief"
        )
    profile = components.default_agent_profile.model_copy(
        update={
            "allowed_skills": tuple(release.ref for release, _ in catalog.entries.values()),
            **(profile_changes or {}),
        }
    )
    service = SkillService(profile, catalog, components.agent_factory.context_planner(profile))
    runtime = SimpleNamespace(context=ctx or context("*"), stream_writer=lambda event: None)
    state = {"messages": [HumanMessage(content="请比较，不能下单", id=user_message_id)]}
    state["skill_state"] = service.binding(runtime, state)
    service.preparer = CandidatePreparation(
        [],
        planner=service.planner,
        system_prompt="",
        tools=(),
        output_schema=None,
        skill_projection=service.projection,
        validator=service.validate_request,
    )
    return service, runtime, state


def activate(service, runtime, state):
    """应用与真实图相同的原生 reducer 候选更新。"""
    return service.preparer.apply(state, service.activate(runtime, state, "market-brief"))


def test_failed_preparation_leaves_state_untouched_and_keeps_real_cost():
    """S21：摘要操作有真实成本，激活失败不提交候选删消息或摘要。"""
    service, runtime, state = service_state()
    old = deepcopy(state)

    class Stage:
        """模拟一次真实摘要成功但未压到完整输入上限的准备步骤。"""

        def before_model(self, candidate, runtime):
            """候选中加入超过总预算的内容并记录真实摘要尝试。"""
            return {
                "messages": [AIMessage(content="过长摘要" * 200000)],
                "summary_calls": 1,
                "context_compaction_attempts": 1,
            }

    service.preparer.stages = (Stage(),)
    with pytest.raises(SkillError) as caught:
        service.activate(runtime, state, "market-brief")
    assert caught.value.code == "SKILL_CONTEXT_BUDGET_EXCEEDED"
    assert caught.value.state_update["summary_calls"] == 1
    assert "messages" not in caught.value.state_update
    assert "skill_state" not in caught.value.state_update
    assert state == old


def test_policy_implicit_only_and_current_scope_authorization():
    """S05/S09：显式策略不能由模型绕过，另一个 scope 不继承激活集合。"""
    service, runtime, state = service_state()
    release, pkg = next(iter(service.catalog.entries.values()))
    catalog = SkillCatalog([(release.model_copy(update={"allow_implicit_invocation": False}), pkg)])
    service, runtime, state = service_state(catalog=catalog)
    assert state["skill_state"]["visible"] == []
    with pytest.raises(SkillError, match="明确选择"):
        service.activate(runtime, state, "market-brief")
    with pytest.raises(SkillError):
        service.activate(runtime, state, "market-brief", explicit=True)
    state["messages"] = [HumanMessage(content="/skill market-brief 请比较", id="user")]
    explicit = service.preparer.apply(
        state, service.activate(runtime, state, "market-brief", explicit=True)
    )
    assert explicit["skill_state"]["active"][0]["activation_source"] == "explicit"
    denied = SimpleNamespace(context=context("artifacts:read"), stream_writer=lambda event: None)
    with pytest.raises(SkillError):
        service.validate_request(denied, explicit, explicit["messages"])
    assert state["skill_state"]["active"] == []


def test_complete_directory_budget_and_explicit_omitted_selection():
    """S13：目录包括说明词也必须在限额内，被省略的发布仍可准确选择。"""
    service, runtime, state = service_state()
    tiny = service.profile.skill_budget.model_copy(update={"catalog_tokens": 1})
    service, runtime, state = service_state(profile_changes={"skill_budget": tiny})
    directory, _, metadata = service.projection(state)
    assert service.planner.counter.text(directory) <= 1
    assert metadata["skill_catalog_omitted"] == 1
    assert activate(service, runtime, state)["skill_state"]["active"]


def test_multibyte_pages_cover_each_character_and_cursor_cannot_cross_snapshot(tmp_path):
    """S12：最终 JSON 字节和 token 双限额，无重字漏字，快照游标不能串包。"""
    service, runtime, state = service_state()
    root = package(tmp_path)
    root.rename(tmp_path / "market-brief")
    root = tmp_path / "market-brief"
    (root / "SKILL.md").write_text(
        "---\nname: market-brief\ndescription: 简报\n---\n阅读 [资料](refs/data.md)。"
    )
    original = '😀中文\\"\n' * 1000
    (root / "refs/data.md").write_text(original)
    pkg = load_package(root)
    release, _ = next(iter(service.catalog.entries.values()))
    ref = release.ref.model_copy(update={"package_hash": pkg.package_hash})
    catalog = SkillCatalog([(release.model_copy(update={"ref": ref}), pkg)])
    service, runtime, state = service_state(
        catalog=catalog, profile_changes={"allowed_skills": (ref,)}
    )
    service.inline_bytes = 1600
    state = activate(service, runtime, state)
    cursor, seen, end = None, "", 0
    while True:
        raw, access = service.read_resource(runtime, state, "market-brief", "refs/data.md", cursor)
        page = json.loads(raw)
        assert len(json.dumps(raw, ensure_ascii=False).encode()) <= 1600
        assert service.planner.counter.text(raw) <= service.profile.skill_budget.page_tokens
        assert page["start"] == end and page["end"] == access["end"]
        seen += page["content"]
        end, cursor = page["end"], page["next_cursor"]
        if cursor is None:
            break
    assert seen == original
    bad = base64.urlsafe_b64encode(json.dumps({"offset": 3, "package": "forged"}).encode()).decode()
    for path, cursor in [("refs/data.md", bad), ("SKILL.md", None), ("../secret", None)]:
        with pytest.raises(SkillError):
            service.read_resource(runtime, state, "market-brief", path, cursor)


def test_artifact_access_inherits_and_cannot_be_laundered(stack):
    """S18/S19：首份归档与回读再归档都保留约束，原始入口缺校验器即拒绝。"""
    _, conversations, ctx, _, artifacts = stack
    ctx = ctx.model_copy(update={"scopes": frozenset({"*"})})
    service, runtime, state = service_state(ctx=ctx)
    state = activate(service, runtime, state)
    raw, ref = service.read_resource(runtime, state, "market-brief", "references/report-format.md")
    message = ToolMessage(
        content=raw,
        name="read_skill_resource",
        tool_call_id="source",
        additional_kwargs={ACCESS_KEY: [ref], RESOURCE_KEY: [ref]},
    )
    archived = ToolResultArchive(artifacts).project(message, ctx)
    assert RESOURCE_KEY not in archived.additional_kwargs
    assert archived.additional_kwargs[ACCESS_KEY] == [ref]
    identity, content_hash = archived.artifact["artifact_id"], archived.artifact["content_hash"]
    authorizer = service.authorizer(runtime, state)
    with pytest.raises(SkillError):
        artifacts.read(identity, context=ctx)
    assert artifacts.read(identity, context=ctx, skill_authorizer=authorizer)
    history = HistoryService(conversations, artifacts)
    page = history.read_artifact(
        ctx, identity, content_hash, mode="json", skill_authorizer=authorizer
    )
    provenance = history.result_provenance(
        ctx, page, tool_name="read_artifact", initial_epoch=0, skill_authorizer=authorizer
    )
    assert provenance[ACCESS_KEY] == [ref]
    second = ToolResultArchive(artifacts).save(
        ToolMessage(
            content=json.dumps(page),
            tool_call_id="second",
            name="derived",
            additional_kwargs=provenance,
        ),
        ctx,
    )
    state["skill_state"]["active"] = []
    for artifact_id in [identity, second["artifact_id"]]:
        with pytest.raises(SkillError):
            artifacts.read(artifact_id, context=ctx, skill_authorizer=authorizer)


def test_manifest_journal_and_bootstrap_preserve_access_dependencies(stack):
    """S20/S26：Manifest 区分正文与资料，Journal 源映射阻止历史恢复洗掉限制。"""
    _, conversations, ctx, identity, _ = stack
    ctx = ctx.model_copy(update={"scopes": frozenset({"*"})})
    service, runtime, state = service_state(ctx=ctx, user_message_id=identity)
    state = activate(service, runtime, state)
    messages = projected_messages(state, skill_projection=service.projection)
    refs = service.validate_request(runtime, state, messages)
    recorder = RequestRecorder(budget(), conversations)
    manifest = recorder.record(ctx, BoundFakeModel(responses=[]), messages, skill_access_refs=refs)
    assert len(manifest.skill_refs) == 1 and not manifest.skill_resource_refs
    assert manifest.skill_catalog_hash
    answer = conversations.append_assistant_message(turn_id=ctx.turn_id, content="DERIVED_SECRET")
    assert answer.skill_access_refs
    historical = history_messages(
        SimpleNamespace(user=None, assistant=answer, status="completed", clarifications=())
    )
    fresh = {
        **state,
        "skill_state": {**state["skill_state"], "active": []},
        "messages": [*historical, HumanMessage(content="新问题", id="new")],
    }
    update = service.sanitize(runtime, fresh)
    clean = service.preparer.apply(fresh, update)
    assert len(clean["messages"]) == 1 and clean["messages"][0].id == "new"


def test_corrupt_binding_and_unpaired_tainted_batch_fail_closed():
    """S11/S20：不同发布或不能安全替换的未完成调用不得继续传输。"""
    service, runtime, state = service_state()
    state = activate(service, runtime, state)
    reference = service.access_ref(runtime, state, state["skill_state"]["active"][0]["skill_ref"])
    state["messages"].append(
        AIMessage(
            content="secret",
            tool_calls=[{"id": "x", "name": "calculate", "args": {}}],
            additional_kwargs={ACCESS_KEY: [reference]},
        )
    )
    state["skill_state"]["active"] = []
    with pytest.raises(SkillError):
        service.sanitize(runtime, state)
    state["skill_state"]["catalog_fingerprint"] = "0" * 64
    with pytest.raises(SkillError, match="版本"):
        service.binding(runtime, state)


def test_real_summary_inherits_sources_and_revocation_stops_summary_transport(stack):
    """S20/S23：真实摘要适配器携带访问来源，调用前撤权时不传输旧资料。"""
    from financeclaw.agent_server.context.compaction import NativeContextMiddleware
    from tests.skills.test_runtime import RecordingModel
    from tests.stage11.test_context_native import draft, messages

    _, _, ctx, _, artifacts = stack
    ctx = ctx.model_copy(update={"scopes": frozenset({"*"})})
    service, runtime, state = service_state(ctx=ctx, user_message_id="real-user")
    state = activate(service, runtime, state)
    ref = service.access_ref(runtime, state, state["skill_state"]["active"][0]["skill_ref"])
    state["messages"] = messages()
    state["messages"][2].additional_kwargs[ACCESS_KEY] = [ref]
    model = RecordingModel(responses=[AIMessage(content=draft())])
    RecordingModel.requests = []
    middleware = NativeContextMiddleware(
        budget(),
        artifacts=artifacts,
        summary_model=model,
        skill_projection=service.projection,
        skill_validator=service.validate_request,
    )
    update = middleware.before_model(state, runtime)
    assert update["summary_calls"] == 1 and update["working_context"]["skill_access_refs"]
    assert len(RecordingModel.requests) == 1
    assert RecordingModel.requests[0][-1].additional_kwargs[ACCESS_KEY]
    prepared = service.preparer.apply(state, update)
    prepared["skill_state"]["active"] = []
    assert service.sanitize(runtime, prepared)["working_context"] is None
    # 在准备摘要之后、实际模型传输之前改变权限。
    state["skill_state"]["active"] = [
        {
            "skill_ref": service.profile.allowed_skills[0].model_dump(mode="json"),
            "activation_source": "model",
        }
    ]
    operation = middleware._operation(state, runtime)
    runtime.context = ctx.model_copy(update={"scopes": frozenset({"artifacts:read"})})
    with pytest.raises(SkillError):
        operation["model"].invoke(operation["prompt"])
    assert len(RecordingModel.requests) == 1


@pytest.mark.parametrize("policy", [{"enabled": False}, {"tenant_allowlist": ("another-tenant",)}])
def test_disabled_or_foreign_tenant_skills_are_unavailable(policy):
    """S05/S09：同名包不能凭猜 ID 绕过禁用或租户白名单。"""
    service, runtime, state = service_state()
    release, pkg = next(iter(service.catalog.entries.values()))
    catalog = SkillCatalog([(release.model_copy(update=policy), pkg)])
    service, runtime, state = service_state(catalog=catalog)
    assert not state["skill_state"]["visible"]
    with pytest.raises(SkillError) as error:
        service.activate(runtime, state, "market-brief", explicit=True)
    assert error.value.code == "SKILL_UNAVAILABLE"


def test_worker_invocation_scope_does_not_inherit_root_or_other_worker_activation():
    """S09/S25：同一 Worker 不同调用身份也必须独立激活，根选择不下传。"""
    from financeclaw.agent_server.tools.subgraph_scope import InvocationScope, active_scope

    ctx = context("*")
    first = InvocationScope(declaration="{}", context=ctx, tool_call_id="first", arguments_hash="a")
    token = active_scope.set(first)
    try:
        service, runtime, state = service_state(
            ctx=ctx, profile_changes={"context_policy": "worker-task-only-v1"}
        )
        assert service.selections(runtime, state) == []
        state = activate(service, runtime, state)
        second = InvocationScope(
            declaration="{}", context=ctx, tool_call_id="second", arguments_hash="a"
        )
        active_scope.set(second)
        with pytest.raises(SkillError):
            service.validate_active(runtime, state)
        assert service.binding(runtime, state)["active"] == []
    finally:
        active_scope.reset(token)


def test_adjacent_pages_merge_dependencies_without_erasing_gaps_or_source_identity():
    """较长资料分页后仍只有有界来源依赖，未读取区间和不同 scope 不能被并入。"""
    from financeclaw.shared.skills.access import merge_access

    service, runtime, state = service_state()
    state = activate(service, runtime, state)
    _, source = service.read_resource(runtime, state, "market-brief", "references/report-format.md")
    pages = [{**source, "start": i, "end": i + 1} for i in range(100)]
    merged = merge_access(pages)
    assert len(merged) == 1 and merged[0]["start"] == 0 and merged[0]["end"] == 100
    disjoint = merge_access(merged, [{**source, "start": 105, "end": 110}])
    assert len(disjoint) == 2
    assert len(merge_access(merged, [{**merged[0], "source_scope": "another"}])) == 2
