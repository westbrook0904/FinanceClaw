"""扩大上下文后验证真实历史装配、预算预留与发布一致性。"""

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from financeclaw.agent_server.bootstrap import build_components
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.catalog import build_release_catalogs


def config(tmp_path, **changes):
    """独立数据库与离线模型，不访问本机凭证或真实模型。"""
    return FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'context.db'}",
        database_auto_create_schema=True,
        artifact_root=str(tmp_path / "artifacts"),
        **changes,
    )


def test_large_context_retains_64_original_messages_above_previous_limit(tmp_path):
    """实际装配超过旧上限的 64 条完整原文，当前输入与预留仍满足预算。"""
    runtime = build_components(config(tmp_path), enable_persistence=True)
    try:
        journal = runtime.conversation_repository
        conversation = journal.create_conversation(
            tenant_id="tenant",
            subject_id="user",
            agent_id="finance_agent",
            agent_profile_version="1.5.0",
        )
        body = "这是需要保留的完整历史原文。" * 400
        for index in range(40):
            turn, _, _ = journal.begin_turn(
                conversation_id=conversation.conversation_id,
                tenant_id="tenant",
                subject_id="user",
                idempotency_key=f"history-{index}",
                request_hash=f"{index:064x}",
                message=f"资料{index}：" + body,
                target_type="agent",
                target_id="finance_agent",
                target_version="1.5.0",
            )
            journal.bind_server_run(turn.turn_id, f"server-{index}", "success")
            journal.append_assistant_message(run_id=turn.run_id, content=f"记录{index}：" + body)
        builder = runtime.context_builder
        messages, selected = builder.build(
            context=ExecutionContext(
                tenant_id="tenant",
                subject_id="user",
                conversation_id=conversation.conversation_id,
                turn_id="current",
                run_id="current",
            ),
            runtime_messages=[HumanMessage(content="继续分析这批资料")],
            system_prompt="基于明确的历史记录回答。",
            tools=[],
        )
        original = journal.list_messages(conversation.conversation_id)
        assert selected.recent_message_ids == tuple(item.message_id for item in original[-64:])
        assert all(
            any(message.content == item.content for message in messages) for item in original[-64:]
        )
        assert selected.input_token_count > 122_768
        assert (
            selected.input_token_count
            + builder.budget.reserved_output_tokens
            + builder.budget.safety_margin
            <= builder.budget.model_input_limit
        )
        assert builder.budget.available_input_tokens == 693_504
        assert messages[-1].content == "继续分析这批资料"
        assert not selected.omissions
    finally:
        runtime.database.close()


@pytest.mark.parametrize("changes", [{"model_max_tokens": 65_536}, {"context_input_limit": 65_536}])
def test_invalid_generation_or_reserve_budget_is_rejected(tmp_path, changes):
    """不能把最大生成量设得高于预留，或让各预留占满上下文。"""
    with pytest.raises(ValidationError, match="context"):
        config(tmp_path, **changes)


def test_history_window_participates_in_shared_release_fingerprint(tmp_path):
    """调整上下文策略会改变发布指纹，防止两端配置不一致后静默恢复。"""
    settings = config(tmp_path)
    first = build_release_catalogs(settings).agent_profiles.resolve("finance_agent", "1.5.0")
    same = build_release_catalogs(config(tmp_path)).agent_profiles.resolve("finance_agent", "1.5.0")
    changed = build_release_catalogs(
        config(tmp_path, context_recent_messages=32)
    ).agent_profiles.resolve("finance_agent", "1.5.0")
    assert first.configuration_fingerprint == same.configuration_fingerprint
    assert first.configuration_fingerprint != changed.configuration_fingerprint
