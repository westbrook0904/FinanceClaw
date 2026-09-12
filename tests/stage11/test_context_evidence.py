"""Stage 11 实际模型用量和派生工件隐私版本的SQL边界回归。"""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from financeclaw.agent_server.context.artifacts import ToolResultArchive
from financeclaw.agent_server.memory.history import HistoryService
from financeclaw.agent_server.middleware.final_context import RequestRecorder
from financeclaw.shared.artifacts.repository import ArtifactNotFound, SqlAlchemyArtifactRepository
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.artifacts.storage import LocalArtifactStore
from financeclaw.shared.conversation.repository import ConversationConflict
from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.repository import lock_owner
from tests.stage3.support import conversation_context, journal
from tests.stage11.test_context_native import budget


@pytest.fixture
def stack(tmp_path):
    """每例创建独立应用SQL文件，仅保存虚构会话和临时工件。"""
    database, conversations = journal(tmp_path / "evidence.db")
    context, identity = conversation_context(conversations, message="虚构测试输入")
    context = context.model_copy(update={"scopes": {*context.scopes, "artifacts:read"}})
    artifacts = ArtifactService(
        SqlAlchemyArtifactRepository(database.session_factory),
        LocalArtifactStore(str(tmp_path / "artifacts")),
    )
    yield database, conversations, context, identity, artifacts
    database.close()


def test_observed_provider_usage_is_separate_from_estimated_manifest(stack):
    """真实usage字段可空且独立于估算，重复回填幂等，冲突回填拒绝。"""
    _, repository, context, identity, _ = stack
    recorder = RequestRecorder(budget(), repository)
    model = FakeMessagesListChatModel(responses=[])
    manifest = recorder.record(context, model, [HumanMessage(content="测试", id=identity)])
    assert manifest.observed_input_tokens is None
    response = AIMessage(
        content="完成", usage_metadata={"input_tokens": 42, "output_tokens": 7, "total_tokens": 49}
    )
    recorder.observe(manifest, response)
    recorder.observe(manifest, response)
    stored = repository.list_manifests(context.conversation_id)[0]
    assert stored.input_token_count == manifest.input_token_count
    assert (stored.observed_input_tokens, stored.observed_output_tokens) == (42, 7)
    with pytest.raises(ConversationConflict, match="conflicting observed usage"):
        repository.record_manifest_usage(manifest.manifest_id, input_tokens=43, output_tokens=7)


def test_derived_artifact_cannot_reinject_forgotten_memory(stack, monkeypatch):
    """S32/S36：读取派生工件前检查epoch，遗忘后不触碰旧物化正文；原始业务回执仍能读。"""
    database, conversations, context, _, artifacts = stack
    actor = MemoryActor(tenant_id=context.tenant_id, subject_id=context.subject_id)
    with database.session_factory.begin() as session:
        lock_owner(session, actor)
    reference = {
        "memory_id": "memory-test",
        "revision": 1,
        "schema_version": 3,
        "memory_type": "profile",
        "injection_reason": "explicit_search",
    }
    message = ToolMessage(
        content="虚构的旧画像" * 300,
        name="search_memories",
        tool_call_id="recall",
        additional_kwargs={
            "memory_derived": True,
            "memory_privacy_epoch": 0,
            "financeclaw_memory_refs": [reference],
        },
    )
    projected = ToolResultArchive(artifacts).project(message, context)
    identity = projected.artifact["artifact_id"]
    assert projected.additional_kwargs["financeclaw_memory_refs"] == [reference]
    assert artifacts.read(identity, context=context)
    metadata = artifacts.repository.get_owned(identity, context.tenant_id, context.subject_id)
    assert metadata.access_policy["memory_privacy_epoch"] == 0
    history = HistoryService(conversations, artifacts)
    result = history.read_artifact(context, identity, metadata.content_hash)
    assert history.result_provenance(context, result, tool_name="read_artifact", initial_epoch=0)[
        "financeclaw_memory_refs"
    ] == [reference]
    business = artifacts.persist(
        {"executed": True, "receipt_id": "order-test"},
        context=context,
        source_type="tool_result",
        source_id="business",
        idempotency_key="business",
    )
    with database.session_factory.begin() as session:
        lock_owner(session, actor).privacy_epoch = 1
    original_get = artifacts.store.get
    reads = []

    def observe_get(uri):
        """断言被遗忘副本在对象存储读取前已被拒绝。"""
        reads.append(uri)
        return original_get(uri)

    monkeypatch.setattr(artifacts.store, "get", observe_get)
    with pytest.raises(ArtifactNotFound, match="privacy change"):
        artifacts.read(identity, context=context)
    assert reads == []
    assert b"order-test" in artifacts.read(business.artifact_id, context=context)
