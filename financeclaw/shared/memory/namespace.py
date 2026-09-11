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
