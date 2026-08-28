"""PlanExecutionState checkpoint 到通用 ExecutionEvent 的转换。"""

from __future__ import annotations

from dataclasses import dataclass

from harness_contracts import (
    JsonValue,
    NodeExecutionStatus,
    PlanExecutionState,
    PlanExecutionStatus,
)
from harness_events import EventPublisher, ExecutionEvent, ExecutionEventName


@dataclass(frozen=True, slots=True)
class EventSpec:
    name: ExecutionEventName
    node_id: str | None = None
    attributes: dict[str, JsonValue] | None = None


class ExecutionEventEmitter:
    """EventPublisher 的 best-effort 适配；StateStore 仍是执行事实来源。"""

    def __init__(self, publisher: EventPublisher) -> None:
        if not isinstance(publisher, EventPublisher):
            raise TypeError("publisher must implement EventPublisher")
        self._publisher = publisher

    @property
    def publisher(self) -> EventPublisher:
        return self._publisher

    async def emit(
        self,
        name: ExecutionEventName,
        *,
        plan_id: str,
        node_id: str | None = None,
        state_version: int | None = None,
        trace_id: str | None = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> bool:
        try:
            await self._publisher.publish(
                ExecutionEvent(
                    name=name,
                    plan_id=plan_id,
                    node_id=node_id,
                    state_version=state_version,
                    trace_id=trace_id,
                    attributes=attributes or {},
                )
            )
        except Exception:
            return False
        return True

    async def emit_checkpoint(
        self,
        previous: PlanExecutionState | None,
        current: PlanExecutionState,
        *,
        trace_id: str | None,
    ) -> tuple[EventSpec, ...]:
        specs = checkpoint_transition_specs(previous, current)
        for spec in specs:
            await self.emit(
                spec.name,
                plan_id=current.plan_id,
                node_id=spec.node_id,
                state_version=current.state_version,
                trace_id=trace_id,
                attributes=spec.attributes,
            )
        checkpoint = EventSpec(
            ExecutionEventName.CHECKPOINT_SAVED,
            attributes={"status": current.status.value},
        )
        await self.emit(
            checkpoint.name,
            plan_id=current.plan_id,
            state_version=current.state_version,
            trace_id=trace_id,
            attributes=checkpoint.attributes,
        )
        return (*specs, checkpoint)


def checkpoint_transition_specs(
    previous: PlanExecutionState | None,
    current: PlanExecutionState,
) -> tuple[EventSpec, ...]:
    """从两个稳定快照推导阶段二最小 Plan/Node 事件。"""

    specs: list[EventSpec] = []
    previous_status = previous.status if previous is not None else None
    if previous is None and current.status is PlanExecutionStatus.CREATED:
        specs.append(EventSpec(ExecutionEventName.PLAN_CREATED))
    elif previous_status is not current.status:
        if current.status is PlanExecutionStatus.RUNNING and previous_status in {
            None,
            PlanExecutionStatus.CREATED,
        }:
            specs.append(EventSpec(ExecutionEventName.PLAN_STARTED))
        elif current.status is PlanExecutionStatus.WAITING:
            specs.append(EventSpec(ExecutionEventName.PLAN_WAITING))
        elif current.status in {PlanExecutionStatus.SUCCEEDED, PlanExecutionStatus.PARTIAL}:
            specs.append(
                EventSpec(
                    ExecutionEventName.PLAN_COMPLETED,
                    attributes={"status": current.status.value},
                )
            )
        elif current.status in {PlanExecutionStatus.FAILED, PlanExecutionStatus.DENIED}:
            specs.append(
                EventSpec(
                    ExecutionEventName.PLAN_FAILED,
                    attributes={"status": current.status.value},
                )
            )
        elif current.status is PlanExecutionStatus.CANCELLED:
            specs.append(EventSpec(ExecutionEventName.PLAN_CANCELLED))

    for node_id, node_state in current.nodes.items():
        old = previous.nodes.get(node_id) if previous is not None else None
        old_status = old.status if old is not None else None
        if (
            node_state.status is NodeExecutionStatus.READY
            and old_status is not NodeExecutionStatus.READY
        ):
            specs.append(EventSpec(ExecutionEventName.NODE_READY, node_id=node_id))
        if (
            node_state.status is NodeExecutionStatus.RUNNING
            and old_status is not NodeExecutionStatus.RUNNING
        ):
            if old_status in {None, NodeExecutionStatus.PENDING}:
                specs.append(EventSpec(ExecutionEventName.NODE_READY, node_id=node_id))
            specs.append(
                EventSpec(
                    ExecutionEventName.NODE_STARTED,
                    node_id=node_id,
                    attributes={"attempt": node_state.attempt},
                )
            )
        elif (
            node_state.status is NodeExecutionStatus.RUNNING
            and old is not None
            and node_state.attempt > old.attempt
        ):
            specs.append(
                EventSpec(
                    ExecutionEventName.NODE_RETRYING,
                    node_id=node_id,
                    attributes={
                        "attempt": old.attempt,
                        "next_attempt": node_state.attempt,
                    },
                )
            )

        if old_status is node_state.status:
            continue
        if node_state.status is NodeExecutionStatus.WAITING:
            specs.append(
                EventSpec(
                    ExecutionEventName.NODE_WAITING,
                    node_id=node_id,
                    attributes={"reason": node_state.waiting_reason or "waiting"},
                )
            )
        elif node_state.status is NodeExecutionStatus.SUCCEEDED:
            specs.append(
                EventSpec(
                    ExecutionEventName.NODE_COMPLETED,
                    node_id=node_id,
                    attributes={"status": "succeeded"},
                )
            )
        elif node_state.status is NodeExecutionStatus.FAILED:
            specs.append(EventSpec(ExecutionEventName.NODE_FAILED, node_id=node_id))
        elif node_state.status is NodeExecutionStatus.DENIED:
            specs.append(EventSpec(ExecutionEventName.NODE_DENIED, node_id=node_id))
        elif node_state.status is NodeExecutionStatus.CANCELLED:
            specs.append(EventSpec(ExecutionEventName.NODE_CANCELLED, node_id=node_id))
        elif node_state.status is NodeExecutionStatus.SKIPPED:
            specs.append(
                EventSpec(
                    ExecutionEventName.NODE_COMPLETED,
                    node_id=node_id,
                    attributes={"status": "skipped"},
                )
            )
    return tuple(specs)
