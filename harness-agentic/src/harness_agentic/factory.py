"""Harness-owned standalone Exploration wrapper template 工厂。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

from harness_contracts import (
    ExplorationBudget,
    ExplorationNodeSpec,
    ExplorationProfile,
    InputBinding,
    NodeOutputBinding,
    PlanNode,
    PlanNodeKind,
    RetryPolicy,
)
from harness_planning import PlanTemplate, PlanValidator

from .profile import ExplorationProfileMaterializer

type ExplorationIdFactory = Callable[[], str]


class ExplorationPlanFactory:
    """只创建单 EXPLORATION node、零 edge 的 identity-free wrapper。"""

    def __init__(
        self,
        profile_materializer: ExplorationProfileMaterializer,
        *,
        exploration_id_factory: ExplorationIdFactory | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        if not isinstance(profile_materializer, ExplorationProfileMaterializer):
            raise TypeError("profile_materializer must be ExplorationProfileMaterializer")
        if exploration_id_factory is not None and not callable(exploration_id_factory):
            raise TypeError("exploration_id_factory must be callable")
        if validator is not None and not isinstance(validator, PlanValidator):
            raise TypeError("validator must be PlanValidator")
        self._profile_materializer = profile_materializer
        self._exploration_id_factory = exploration_id_factory or (
            lambda: f"exploration-{uuid4().hex}"
        )
        self._validator = validator or PlanValidator(profile_materializer.catalog)

    @property
    def profile_materializer(self) -> ExplorationProfileMaterializer:
        return self._profile_materializer

    def create_template(
        self,
        profile: ExplorationProfile,
        *,
        goal_bindings: Mapping[str, InputBinding],
        budget: ExplorationBudget | None = None,
    ) -> PlanTemplate:
        if not isinstance(goal_bindings, Mapping):
            raise TypeError("goal_bindings must be a mapping")
        exploration_id = self._exploration_id_factory()
        if not isinstance(exploration_id, str) or not exploration_id.strip():
            raise ValueError("exploration identity factory must return a non-empty string")
        if exploration_id != exploration_id.strip():
            raise ValueError("exploration identity must not contain surrounding whitespace")
        profile_snapshot = self._profile_materializer.materialize(profile, budget=budget)
        node_id = "exploration"
        template = PlanTemplate(
            nodes=(
                PlanNode(
                    node_id=node_id,
                    kind=PlanNodeKind.EXPLORATION,
                    exploration=ExplorationNodeSpec(
                        exploration_id=exploration_id,
                        goal_bindings=dict(goal_bindings),
                        profile=profile_snapshot,
                    ),
                    retry_policy=RetryPolicy(max_attempts=1),
                ),
            ),
            edges=(),
            outputs={"result": NodeOutputBinding(node_id=node_id, pointer="/output")},
        )
        return self._validator.validate_template(template, executable=False)
