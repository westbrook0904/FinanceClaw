# Environment profiles

These examples define policy, not credentials. Render the selected profile into
the deployment environment and inject every secret from a Secret Manager.
Development may use the static bearer adapter; staging and production must use
OIDC, PostgreSQL, internal Agent Server service auth and S3-compatible artifacts.
