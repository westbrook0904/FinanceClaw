"""Authenticated memory management over the shared transactional domain."""

from pydantic import ValidationError

from financeclaw.shared.memory.authorization import require_scope
from financeclaw.shared.memory.models import MemoryActor, MemoryConflict, MemoryMutation
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import MemoryRepository


class InvalidMemoryInput(ValueError):
    """A bounded public request does not match the memory content contract."""


def user_actor(user, *, processing_region="global"):
    """Extract ownership and current permissions exclusively from authenticated credentials."""
    return MemoryActor(
        tenant_id=user.tenant_id,
        subject_id=user.subject_id,
        scopes=user.scopes,
        kind="user",
        data_classification="confidential",
        processing_region=processing_region,
    )


class MemoryManagementService:
    """Keep HTTP shapes out of the memory domain and never manufacture execution identity."""

    def __init__(self, sessions, settings):
        """Bind authority and explicit deployment policy without creating a model client."""
        self.repository = MemoryRepository(sessions)
        self.mutations = MemoryMutationService(
            sessions,
            auto_commit_low_risk=settings.memory_auto_commit_low_risk_preferences,
            candidate_seconds=settings.memory_candidate_seconds,
        )
        self.config = settings

    def settings(self, user):
        """Return effective feature switches and independent background progress."""
        actor = user_actor(user, processing_region=self.config.processing_region)
        require_scope(actor, "memory:read")
        snapshot = self.repository.owner_snapshot(actor).model_dump(mode="json")
        return {
            **snapshot,
            "enabled": self.config.memory_enabled,
            "auto_extract": self.config.memory_enabled
            and self.config.memory_auto_extract
            and snapshot["auto_enabled"],
            "auto_commit_low_risk_preferences": self.config.memory_auto_commit_low_risk_preferences,
        }

    def update_settings(self, user, request, *, key):
        """Change owner policy with a permanent idempotency receipt."""
        if request.read_enabled is None and request.auto_extract is None:
            raise InvalidMemoryInput("at least one memory setting is required")
        return self.mutations.update_settings(
            user_actor(user, processing_region=self.config.processing_region),
            mutation_id=key,
            expected_policy_revision=request.expected_revision,
            read_enabled=request.read_enabled,
            auto_enabled=request.auto_extract,
        )

    def list(
        self,
        user,
        *,
        kind=None,
        candidates=False,
        limit=50,
        after=None,
        status="active",
        scope_type=None,
        scope_id=None,
    ):
        """Return bounded current facts without deleted bodies or cross-owner results."""
        rows, cursor = self.repository.list_page(
            user_actor(user, processing_region=self.config.processing_region),
            kind=kind,
            status="proposed" if candidates else status,
            scope_type=scope_type,
            scope_id=scope_id,
            limit=limit,
            after=after,
        )
        return {"items": rows, "next_cursor": cursor}

    def get(self, user, memory_id):
        """Use identical not-found behavior for foreign and absent records."""
        return self.repository.get(
            user_actor(user, processing_region=self.config.processing_region),
            memory_id,
            include_candidates=True,
        )

    def write(self, user, request, *, key, memory_id=None):
        """Register direct user evidence in the same transaction as the fact or proposal."""
        if not self.config.memory_enabled:
            raise PermissionError("memory is disabled by deployment policy")
        try:
            mutation = MemoryMutation(
                mutation_id=key,
                operation="update" if memory_id else "create",
                memory_id=memory_id,
                expected_revision=getattr(request, "expected_revision", None),
                kind=request.kind,
                field=request.field,
                content=request.content,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                expires_at=request.valid_until,
                explicit_intent=True,
            )
        except (ValidationError, ValueError) as exc:
            raise InvalidMemoryInput("invalid profile field, scope or update target") from exc
        try:
            return self.mutations.apply(
                user_actor(user, processing_region=self.config.processing_region), mutation
            )
        except MemoryConflict:
            raise
        except ValueError as exc:
            raise InvalidMemoryInput(
                "memory content does not match the registered field policy"
            ) from exc

    def forget(self, user, memory_id, request, *, key):
        """Forget an authenticated explicit ID/version without an additional candidate."""
        return self.mutations.apply(
            user_actor(user, processing_region=self.config.processing_region),
            MemoryMutation(
                mutation_id=key,
                operation="forget",
                memory_id=memory_id,
                expected_revision=request.expected_revision,
                explicit_intent=True,
            ),
        )

    def decide(self, user, candidate_id, request, *, key):
        """Require the permission for the candidate operation under its exact reviewed hash."""
        if not self.config.memory_enabled and request.decision == "approve":
            from financeclaw.shared.memory.repository import current_record

            with self.repository.sessions() as session:
                candidate = current_record(
                    session,
                    user_actor(user, processing_region=self.config.processing_region),
                    candidate_id,
                )
                if candidate is not None and candidate.operation != "forget":
                    raise PermissionError("memory is disabled by deployment policy")
        return self.mutations.decide(
            user_actor(user, processing_region=self.config.processing_region),
            candidate_id,
            request.decision,
            key,
            request.expected_revision,
            request.content_hash,
        )
