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
