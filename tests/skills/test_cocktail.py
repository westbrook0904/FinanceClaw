"""固定第三方调酒包及其原生图、HTTP、飞书受理入口的集成回归。"""

import json
from hashlib import sha1

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from financeclaw.api.application.conversation_service import ConversationService
from financeclaw.api.application.feishu_channel_service import FeishuChannelService
from financeclaw.api.http.app import create_app
from financeclaw.api.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.kernel.skills import SkillError, SkillRef
from financeclaw.shared.llm.budget import TokenCounter
from financeclaw.shared.releases.skills import builtin_skill_release, builtin_skills
from financeclaw.shared.skills.access import ACCESS_KEY
from financeclaw.shared.turns.tables import ConversationTurnRow
from tests.skills.test_runtime import RecordingModel, call, setup
from tests.stage1.test_agent import components_with_tools, context
from tests.stage8_hotfix.test_feishu_clarification import Replies, message
from tests.stage10.runtime import OWNER
from tests.stage10.runtime import runtime as runtime

SKILL_ID = "cocktail-from-what-i-have"
TASK = "我有金酒、柠檬、苏打水、蜂蜜和冰块，没有摇壶，做两杯清爽的，也给一个无酒精版本。"
COMMAND = f"/skill {SKILL_ID} {TASK}"
SCOPES = frozenset({"tools:read"})


def test_vendored_original_and_license_match_pinned_upstream_blobs():
    """固定提交、原文和许可逐字节可核对，避免把改写内容冒充原版包。"""
    catalog = builtin_skills()
    release, package = next(
        entry for entry in catalog.entries.values() if entry[0].ref.skill_id == SKILL_ID
    )
    provenance = json.loads(package.contents["SOURCE.json"])
    assert provenance["commit"] == "5a0326ce34b44c015fc26b5c28f6118092c806e4"
    assert provenance["local_version"] == release.ref.version == "1.0.0"
    assert provenance["license"] == "MIT" and provenance["modifications"] == []
    for path, key in (("SKILL.md", "git_blob_sha"), ("LICENSE", "license_git_blob_sha")):
        content = package.contents[path]
        blob = b"blob " + str(len(content)).encode() + b"\0" + content
        assert sha1(blob, usedforsecurity=False).hexdigest() == provenance[key]


def test_cocktail_release_has_no_financial_dependency_or_permission_escalation():
    """调酒方法无需行情权限；原有行情技能和未审阅目录继续独立拒绝。"""
    components, _ = components_with_tools()
    profile, catalog = components.default_agent_profile, builtin_skills()
    release, package = catalog.authorize(profile, context(*SCOPES), SKILL_ID, invoke=True)
    assert not release.required_tools and not release.required_scopes
    assert release.allow_implicit_invocation and package.implicit
    method_only = profile.model_copy(update={"allowed_skills": (release.ref,), "allowed_tools": ()})
    catalog.validate_profile(method_only)
    with pytest.raises(SkillError):
        catalog.authorize(profile, context(*SCOPES), "market-brief", invoke=True)
    with pytest.raises(ValueError, match="no reviewed platform release"):
        builtin_skill_release(
            SkillRef(skill_id="unreviewed", version="1.0.0", package_hash="a" * 64)
        )
    assert TokenCounter("utf8-bytes-v1").text(package.body) <= profile.skill_budget.body_tokens


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("empty_tokenizer_cache", [False, True])
@pytest.mark.asyncio
async def test_cocktail_loads_complete_original_without_market_scope(
    monkeypatch, explicit, empty_tokenizer_cache
):
    """替换外部模型后运行真实图，验证两种激活方式及无分词缓存的完整投影。"""
    if empty_tokenizer_cache:
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    responses = [] if explicit else [call("load_skill", {"skill_id": SKILL_ID}, "load")]
    graph, _ = setup([*responses, AIMessage(content="调酒回答占位，仅用于协议测试。")])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=COMMAND if explicit else TASK, id="user")]},
        {"configurable": {"thread_id": f"cocktail-{explicit}-{empty_tokenizer_cache}"}},
        context=context(*SCOPES),
    )
    active = result["skill_state"]["active"]
    assert len(active) == 1 and active[0]["skill_ref"]["skill_id"] == SKILL_ID
    assert active[0]["activation_source"] == ("explicit" if explicit else "model")
    _, package = builtin_skills().resolve(active[0]["skill_ref"])
    for index, request in enumerate(RecordingModel.requests):
        bodies = [
            item
            for item in request
            if item.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
        ]
        assert len(bodies) == int(explicit or index > 0)
        if bodies:
            assert bodies[0].content.split("\n", 1)[1] == package.body
    assert SKILL_ID in str(RecordingModel.requests[0][0].content)
    assert all("market_snapshot" not in names for names in RecordingModel.bindings)
    assert not any(
        item.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
        for item in result["messages"]
    )
    assert result["messages"][-1].additional_kwargs[ACCESS_KEY][0]["ref"]["skill_id"] == SKILL_ID


@pytest.mark.parametrize("entrypoint", ["http", "feishu"])
@pytest.mark.asyncio
async def test_same_cocktail_command_pins_release_through_http_and_feishu(runtime, entrypoint):
    """仅替换外部传输，真实受理事务固定同一技能且重放不会新增任务。"""
    service = runtime.turns
    conversations = ConversationService(service.journal, service.releases.agents, turns=service)
    if entrypoint == "http":
        app = create_app(
            turns=service,
            conversations=conversations,
            authenticator=StaticBearerAuthenticator(
                {"owner": AuthenticatedPrincipal(**OWNER, scopes=SCOPES)}
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            created = await client.post("/v1/conversations", json={})
            assert created.status_code == 201
            base = f"/v1/conversations/{created.json()['conversation_id']}"
            for replay in (False, True):
                response = await client.post(
                    base + "/turns",
                    headers={"Idempotency-Key": "cocktail-example"},
                    json={"message": COMMAND},
                )
                assert response.status_code == 202
                assert response.json()["idempotent_replay"] is replay
            status = await client.get(base + "/turns/" + response.json()["turn_id"])
            assert status.status_code == 200 and status.json()["status"] == "accepted"
    else:
        channel = FeishuChannelService(
            conversations, app_id="app", allowed_open_ids=frozenset({"user"}), scopes=SCOPES
        )
        replies = Replies()
        for _ in range(2):
            assert await channel.process(message(COMMAND), replies) == "accepted"
        assert not replies.texts
    with service.sessions() as session:
        turns = session.scalars(select(ConversationTurnRow)).all()
        assert len(turns) == 1
        turn = turns[0]
        assert set(turn.grant_scopes) == SCOPES
        selection = turn.release_snapshot["requested_skills"]
        assert len(selection) == 1 and selection[0]["skill_id"] == SKILL_ID
        builtin_skills().resolve(selection[0])
        journal = service.journal.messages_for_turn(turn.conversation_id, turn.turn_id)
        assert journal[0].content == COMMAND
    assert not runtime.client.calls
