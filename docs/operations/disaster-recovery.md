# Disaster recovery and restore evidence

Target objectives must be approved per deployment. Initial engineering targets
are application/Agent PostgreSQL RPO 15 minutes and RTO 60 minutes, Artifact
Store RPO 15 minutes and RTO 120 minutes, and Redis RPO 0 because it is not the
system of record.

A quarterly exercise must restore both PostgreSQL databases and a versioned
Artifact Store snapshot into an isolated account, run Alembic consistency
checks, verify a pre-existing conversation and interrupted workflow, verify an
artifact hash, and drain a copied outbox without duplicate external effects.
Record backup identifiers, timestamps, measured RPO/RTO, hashes, approver and
follow-up actions. A successful backup job without a restore is not evidence.
