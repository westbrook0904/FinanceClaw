# Stage-5 release checklist

- [ ] Agent Server deployment/licensing, residency and support boundary approved.
- [ ] OIDC issuer/audience/JWKS and asymmetric algorithm allowlist verified.
- [ ] BFF is the only public route; Agent Server service identity tested.
- [ ] PostgreSQL migrations and forward-only rollback procedure rehearsed.
- [ ] S3 versioning, encryption, retention and owner-scoped keys verified.
- [ ] OTel backend, dashboards, SLO alerts and outbox backlog alerts verified.
- [ ] Versioned LangSmith offline dataset passes; online sampling is non-blocking.
- [ ] Cross-tenant, prompt injection, tool authorization and secret-leak tests pass.
- [ ] Load/capacity and provider/network fault injection meet approved SLOs.
- [ ] Backup restore evidence meets approved RPO/RTO.
- [ ] SBOM and dependency vulnerability scan have no unaccepted high-risk finding.
- [ ] Security/threat-model review closes every high-risk item.
