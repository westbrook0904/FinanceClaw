# Production runbook

## Boundaries

Only the FinanceClaw BFF is public. The Agent Server, application PostgreSQL,
Agent Server PostgreSQL, Redis and Artifact Store are private services. The BFF
accepts OIDC access tokens and derives tenant, subject and scopes from verified
claims; request bodies never supply identity.

## Deploy

1. Resolve credentials from the organization Secret Manager and render the
   environment without persisting values in CI logs.
2. Run `alembic upgrade head` as a one-shot migration job.
3. Verify `/health`, then `/ready`; readiness requires PostgreSQL, Artifact Store
   and Agent Server.
4. Run the versioned Stage-5 regression experiment. Critical security cases and
   the configured aggregate baseline must pass.
5. Roll out gradually while watching request latency, Agent Server queue/run
   duration, model/tool counts, context budget, token/cost and outbox backlog.

## Rollback

Roll application code back without downgrading a destructive schema migration.
Stage-5 migration is additive; the previous binary ignores `outbox_events`.
Pause publishers before rollback and retain pending events. Feature flags require
an owner and removal date.

## Incidents

- Provider unavailable: bounded SDK retries, then return a visible dependency
  error; never claim that a financial action completed.
- LangSmith unavailable: continue the request, emit OTel/log alert and preserve
  formal Audit. Evaluation services are never request-path dependencies.
- Agent Server unavailable: `/ready` returns 503; stop new traffic and let the
  durable server resume in-flight workflows after recovery.
- Outbox failures: inspect `dead_letter`, repair the sink, return rows to pending
  through an audited administrative procedure.
- Suspected secret leak: rotate first, restrict trace/log access, run the leak
  scan, preserve incident evidence and follow the data-request runbook.
