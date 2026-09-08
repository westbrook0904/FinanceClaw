"""跨服务回归测试的组合夹具；生产入口各自使用所属服务的 bootstrap。"""

from dataclasses import fields
from types import SimpleNamespace

from financeclaw.agent_server.bootstrap import build_components as build_agent_components
from financeclaw.coordination.delegation.repository import SqlAlchemyDelegationRepository
from financeclaw.coordination.workflows.repository import SqlAlchemyWorkflowRepository


def build_components(*args, **kwargs):
    """给既有跨服务测试补齐协调侧仓储，保持共享同一个 Session 工厂。"""
    runtime = build_agent_components(*args, **kwargs)
    sessions = runtime.database.session_factory if runtime.database else None
    return SimpleNamespace(
        **{field.name: getattr(runtime, field.name) for field in fields(runtime)},
        default_agent_profile=runtime.default_agent_profile,
        delegation_repository=SqlAlchemyDelegationRepository(sessions) if sessions else None,
        workflow_repository=SqlAlchemyWorkflowRepository(sessions) if sessions else None,
    )
