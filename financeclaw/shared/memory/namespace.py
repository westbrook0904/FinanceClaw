"""Canonical Store namespaces from trusted tenant and subject identities."""

import base64


def owner_namespace(context):
    """Encode reserved characters without weakening exact tenant/subject boundaries."""
    labels = [
        base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        for value in (context.tenant_id, context.subject_id)
    ]
    return ("financeclaw", "v2", *labels)


def history_namespace(context, conversation_id=None):
    """History indexing requires a conversation; execution may search the owner's prefix."""
    base = (*owner_namespace(context), "history")
    return (*base, conversation_id) if conversation_id else base


def memory_index_namespace(context, index_version: str = "memory-v1"):
    """Use exact encoded owner labels and a separate rebuildable v3 index root."""
    labels = [
        base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        for value in (context.tenant_id, context.subject_id)
    ]
    if not index_version or "/" in index_version or len(index_version) > 64:
        raise ValueError("invalid memory index version")
    return ("financeclaw", "v3", *labels, "memory_index", index_version)
