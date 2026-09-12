"""Native Store discovers IDs; SQL validates every version before returning memory text."""

import logging
import re
from time import monotonic

from financeclaw.shared.memory.models import MemoryNotFound
from financeclaw.shared.memory.namespace import memory_index_namespace

LOGGER = logging.getLogger(__name__)


def needs_task_recall(message: str) -> bool:
    """Recognize explicit historical continuation without another classification model call."""
    return bool(
        re.search(
            r"之前|上次|此前|记得|记忆|继续|曾经|历史|previous|last time|remember|continue",
            message,
            re.I,
        )
    )


def bounded_query(text: str, limit=512) -> str:
    """Preserve the initial task and latest corrections when forming a bounded search query."""
    from financeclaw.shared.infrastructure.security.redaction import redact_sensitive

    text = redact_sensitive(text)
    if len(text) <= limit:
        return text
    head = limit // 2
    return text[:head] + "\n…\n" + text[-(limit - head - 3) :]


class MemoryRecall:
    """Provide bounded semantic recall with a SQL-only degradation path."""

    def __init__(self, repository, *, index_version):
        """Use the same index revision as integrations and a shared authority repository."""
        self.repository, self.index_version = repository, index_version

    def search(self, actor, store, *, query=None, limit=6):
        """Never return body text from Store, including stale and foreign indexed items."""
        started = monotonic()
        fallback_reason = "store_unavailable" if store is None else "no_semantic_query"
        limit = max(1, min(limit, 20))
        if store is not None and query:
            try:
                items = store.search(
                    memory_index_namespace(actor, self.index_version),
                    query=bounded_query(query),
                    limit=limit * 3,
                )
            except Exception:
                items = None
                fallback_reason = "store_error"
            if items is not None:
                fallback_reason = "index_empty" if not items else "index_filtered"
                result, seen = [], set()
                with self.repository.sessions() as session:
                    for item in items:
                        value = item.value
                        identity, revision = value.get("memory_id"), value.get("revision")
                        if (
                            not isinstance(identity, str)
                            or not isinstance(revision, int)
                            or identity in seen
                        ):
                            continue
                        try:
                            record = self.repository.get_in_session(session, actor, identity)
                        except MemoryNotFound:
                            continue
                        if record.kind != "task" or record.revision != revision:
                            continue
                        seen.add(identity)
                        result.append(record)
                        if len(result) == limit:
                            break
                if result:
                    return tuple(result)
        rows = self.repository.list_records(actor, kind="task", limit=100)
        if not query:
            self._fallback_log(fallback_reason, started, len(rows), min(limit, len(rows)))
            return rows[:limit]
        # Chinese words have no spaces; bigrams also match corrected natural-language queries.
        terms = re.findall(r"[a-z0-9_]+", query.lower())
        for phrase in re.findall(r"[\u4e00-\u9fff]+", query):
            terms.extend(phrase[index : index + 2] for index in range(max(1, len(phrase) - 1)))
        ranked = sorted(
            rows,
            key=lambda row: sum(term in row.content.lower() for term in terms),
            reverse=True,
        )
        result = tuple(row for row in ranked if any(term in row.content.lower() for term in terms))[
            :limit
        ]
        self._fallback_log(fallback_reason, started, len(rows), len(result))
        return result

    @staticmethod
    def _fallback_log(reason, started, candidates, results):
        """Expose degradation and elapsed time without query, owner, body or raw error text."""
        LOGGER.info(
            "memory_recall_fallback reason=%s elapsed_seconds=%.3f candidates=%d results=%d",
            reason,
            monotonic() - started,
            candidates,
            results,
        )
