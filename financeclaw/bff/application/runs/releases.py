"""BFF admission uses immutable root and Worker declarations without execution graphs."""

from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.snapshots import verify_agent_snapshot


class BFFReleases:
    """Resolve exact root releases; conversations pin their root release."""

    def __init__(self, catalogs):
        """Retain static catalogs only; never construct an AgentFactory in BFF."""
        self.agents = catalogs.agent_profiles
        self.tools = catalogs.tool_catalog
        self.workflows = catalogs.workflow_catalog

    def verify(self, snapshot):
        """Unavailable or altered pinned releases cannot resume using latest code."""
        try:
            profile = self.agents.resolve(
                snapshot["profile"]["agent_id"], snapshot["profile"]["version"]
            )
        except LookupError as exc:
            raise ExecutionConflict("pinned BFF release is unavailable") from exc
        verify_agent_snapshot(profile, snapshot)
        self.require_root(profile)
        return profile

    @staticmethod
    def require_root(profile):
        """Accept a top-level release with internal Workers only."""
        if profile.agent_id != "finance_agent" or not profile.worker_manifest:
            raise ExecutionConflict("conversation requires explicit published root release")
