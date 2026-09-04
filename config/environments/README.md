# Environment profiles

These examples define policy, not credentials. Render the selected profile into
the deployment environment and inject every secret from a Secret Manager.
Development may use the static bearer adapter; staging and production must use
OIDC, PostgreSQL, internal Agent Server service auth and S3-compatible artifacts.

The Feishu P2P channel is disabled in every profile by default. Enable it only
on one BFF instance, inject the app secret externally, configure a non-empty
`FINANCECLAW_FEISHU_ALLOWED_OPEN_IDS` canary list, and use `strict` security mode
when enabling it in production.
