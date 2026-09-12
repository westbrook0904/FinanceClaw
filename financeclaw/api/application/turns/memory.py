"""Memory source and closure hooks that share the existing business transaction."""

from financeclaw.shared.memory.lifecycle import revoke_turn_sources_in_session
from financeclaw.shared.memory.models import MemoryActor


def actor_for_turn(turn, scopes=None):
    """Use committed Turn ownership and server-verified current input scopes."""
    return MemoryActor(
        tenant_id=turn.tenant_id,
        subject_id=turn.subject_id,
        scopes=frozenset(turn.grant_scopes if scopes is None else scopes),
        kind="user",
        turn_id=turn.turn_id,
        conversation_id=turn.conversation_id,
        agent_id=turn.release_snapshot.get("profile", {}).get("agent_id"),
        data_classification=turn.release_snapshot.get("profile", {}).get(
            "data_classification", "internal"
        ),
        processing_region=turn.release_snapshot.get("context", {}).get(
            "processing_region", "global"
        ),
    )


def accepts_memory(service, turn):
    """Honor deployment and the pinned Agent policy on every input channel."""
    return (
        service.settings.memory_enabled
        and turn.release_snapshot.get("profile", {}).get("memory_policy", "none") != "none"
    )


def register_message(service, session, turn):
    """Register original evidence only after its Journal row exists."""
    if accepts_memory(service, turn):
        service.memory_intake.register_message_in_session(
            session,
            actor_for_turn(turn),
            turn.user_message_id,
            allow_derivation=service.settings.memory_auto_extract,
        )


def register_answer(service, session, turn, interaction, scopes):
    """Bind accepted schema-aware answers; an approval button is not profile evidence."""
    if accepts_memory(service, turn) and interaction.kind != "approval":
        service.memory_intake.register_interaction_in_session(
            session,
            actor_for_turn(turn, scopes),
            interaction.interaction_id,
            allow_derivation=service.settings.memory_auto_extract,
        )


def close_turn(service, session, turn):
    """Commit extraction intent alongside completed product state and the final Journal."""
    if accepts_memory(service, turn) and service.settings.memory_auto_extract:
        return service.memory_intake.close_turn_in_session(
            session,
            actor_for_turn(turn),
            turn.turn_id,
            model_profile_version=service.memory_profile_fingerprint,
        )
    return None


def revoke_derivation(service, session, turn):
    """Explicit Turn revocation cancels outstanding source duties without undoing facts."""
    if accepts_memory(service, turn):
        revoke_turn_sources_in_session(session, actor_for_turn(turn), turn.turn_id)
