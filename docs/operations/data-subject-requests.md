# User data request procedure

Identity and tenant ownership must be re-verified before export, revocation or
deletion. Search FinanceClaw application tables by trusted tenant and subject,
then coordinate the matching Agent Server Store/checkpoints, Artifact Store and
LangSmith retention APIs. Never search by a subject supplied only in a request
body.

Exports contain business records and references, not credentials or unrelated
Audit subjects. Deletion removes mutable conversation, memory and artifact data;
append-only Audit is retained or pseudonymized according to legal policy. Record
the request identifier, verified owner hash, systems queried, object counts,
exceptions, operator, approver and completion timestamps in the evidence case.

## Stage 9 memory operations

`forget_memory(mode="revoke")` makes the Store record inactive and removes it
from normal retrieval. `mode="delete"` uses native `Store.delete` to remove the
specified profile/event body and vector index. Before attempting deletion, the
service persists a `memory_delete` outbox task containing the target mutation ID
and a body-free audit receipt. A Store or audit failure remains an unfinished
operation; the memory worker retries it. Do not report physical completion when
the tool returns an error. Replaying an older deletion does not remove a newly
saved value with a different mutation ID.

Successful forgetting, including a Store change with an unfinished audit receipt, invalidates the current recall, discards the mixed working
summary, and resets intermediate recalled explanations while preserving the user
request and deletion receipt. This affects the next model request. It does not
erase original Journal messages, earlier checkpoints, tool snapshots or existing
trace copies. Tool snapshots are exact payloads returned at execution time; a URL
inside a payload is not a snapshot of the remotely linked document.

For a complete subject erasure case, stop admission for the verified subject and
settle active runs/interrupts first. Hide/delete the owned Journal sources before
purging derived history indexes, so an old indexing event cannot recreate visible
history. Remove that owner's `financeclaw/v2/...` Store namespaces, all native
threads referenced by their execution snapshots, and Artifact objects plus their
retained versions under the configured object-store retention policy. Coordinate
LangSmith/provider/log deletion separately and record each system's completion or
exception in the existing data-request case. This remains an operator procedure;
there is no model-callable bulk `delete_subject_data` tool.

Checkpoint pruning and expired-artifact cleanup have explicit preview/apply
commands documented in [context and memory operations](context-budget.md). They
are retention operations, not a substitute for a complete erasure case. They hold
the affected conversation lock while checking references and performing the
bounded native/object-store operation; do not run them as an unbounded foreground
request. Production object stores with versioning require a matching policy for
old object versions before declaring all physical copies erased.
