# Environment profiles

These examples define policy, not credentials. Render the selected profile into
the deployment environment and inject every secret from a Secret Manager.
Development may use the static bearer adapter; staging and production must use
OIDC, PostgreSQL, internal Agent Server service auth and S3-compatible artifacts.

The Feishu P2P channel is disabled in every profile by default. Enable it only
on one BFF instance, inject the app secret externally, configure a non-empty
`FINANCECLAW_FEISHU_ALLOWED_OPEN_IDS` canary list, and use `strict` security mode
when enabling it in production.

The Stage 7 Ziwei candidate is also disabled by default and restricted to
development/test. It requires an explicit candidate convention, an externally
provided HMAC key, hidden LangSmith inputs/outputs and disabled full-I/O debugging.
Do not enable it for real personal data yet. Follow
[`docs/operations/ziwei-agent.md`](../../docs/operations/ziwei-agent.md) for matching
BFF/Agent Server configuration and explicit `ziwei:read` authorization.
