#!/usr/bin/env python3
"""Summarize actual Surefire/Failsafe results; refuse missing or failing evidence."""
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
suites = []
report_paths = []
for kind, directory in [("unit", "surefire-reports"), ("integration", "failsafe-reports")]:
    reports = sorted((root / "target" / directory).glob("TEST-*.xml"))
    if not reports:
        raise SystemExit(f"Missing {kind} test evidence; run scripts/mvn.sh verify")
    for report in reports:
        report_paths.append(report)
        suite = ET.parse(report).getroot()
        entry = {"kind": kind, "suite": suite.attrib["name"], "tests": int(suite.attrib["tests"]),
                 "failures": int(suite.attrib["failures"]), "errors": int(suite.attrib["errors"]),
                 "skipped": int(suite.attrib["skipped"]), "seconds": float(suite.attrib["time"]),
                 "cases": [case.attrib["name"] for case in suite.findall("testcase")]}
        if not entry["tests"] or entry["failures"] or entry["errors"] or entry["skipped"]:
            raise SystemExit(f"Incomplete or failed evidence: {entry['suite']}")
        suites.append(entry)
expected = {".".join(p.relative_to(root / "src/test/java").with_suffix("").parts)
            for pattern in ("*Test.java", "*IT.java") for p in (root / "src/test/java").rglob(pattern)}
if {s["suite"] for s in suites} != expected:
    raise SystemExit("Report suites do not match current tests; run a clean verify")
summary = ET.parse(root / "target/failsafe-reports/failsafe-summary.xml").getroot()
if (summary.attrib.get("result") not in {"0", "null"}
        or summary.attrib.get("timeout") != "false"
        or any(summary.findtext(key) != "0" for key in ("errors", "failures", "skipped", "flakes"))
        or int(summary.findtext("completed", "0")) != sum(s["tests"] for s in suites if s["kind"] == "integration")):
    raise SystemExit("Failsafe did not report a successful run")
paths = sorted(set([root / "pom.xml", root / "toolchains.lock.json", root / "ziwei-dependencies.lock.json", root / "THIRD_PARTY_NOTICES.md", root / "mvnw",
                *root.glob(".mvn/**/*properties"), *root.glob(".mvn/**/*.xml"), *root.glob("src/**/*.java"),
                *[p for d in ("src/main/resources", "src/test/resources") for p in (root / d).rglob("*") if p.is_file()],
                *root.glob("scripts/*.py"), *root.glob("scripts/*.sh"), *root.glob("scripts/*.java"), *root.glob("ops/*.sql"), *root.glob("ops/*.sh"), *root.glob("ops/*.json"), *root.glob(".github/workflows/*.yml"), root / "compose.yml", root / "Dockerfile", root / ".dockerignore", root / ".env.example"]))
# Delivery scripts have their own Compose evidence. Fingerprint them, but only Java build inputs
# participate in the Java report freshness check; editing a deployment script does not rerun Java.
test_inputs = [p for p in paths if p.relative_to(root).parts[0] in {"src", ".mvn"}
               or p.name in {"pom.xml", "toolchains.lock.json", "ziwei-dependencies.lock.json", "mvnw", "mvn.sh"}]
if any(p.stat().st_mtime > min(r.stat().st_mtime for r in report_paths)
       for p in test_inputs):
    raise SystemExit("Sources changed after tests started; run verify again before collecting evidence")
manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
validator = json.loads((root / "target/m6/live-validator-tests.json").read_text())
if (validator['tests'] != 8 or any(validator[k] for k in ('failures', 'errors', 'skipped'))
        or any(manifest['scripts/' + name] != value for name, value in validator['source_sha256'].items())):
    raise SystemExit('Live validator contract evidence is stale or failed; run scripts/test-live-acceptance.py')
packaging = json.loads((root / "target/m6/runtime-packaging.json").read_text())
if packaging['application_jar_sha256'] != hashlib.sha256((root / "target/financeclaw-java-0.1.0-SNAPSHOT.jar").read_bytes()).hexdigest():
    raise SystemExit('Ziwei runtime image evidence does not match the current jar')
preflight = json.loads((root / "target/m5d/preflight-tests.json").read_text())
if (preflight['tests'] != 12 or any(preflight[k] for k in ('failures', 'errors', 'skipped'))
        or any(manifest['scripts/' + name] != value for name, value in preflight['source_sha256'].items())):
    raise SystemExit('Drain preflight evidence is stale or failed; run scripts/test-inspect-drain.py')
privacy = json.loads((root / "target/m5e/privacy-tests.json").read_text())
if (privacy['tests'] != 13 or any(privacy[k] for k in ('failures', 'errors', 'skipped'))
        or any(manifest[name] != value for name, value in privacy['source_sha256'].items())):
    raise SystemExit('Restore privacy evidence is stale or failed; run scripts/test-restore-privacy.py')
evidence = {"base_commit": "993e7acded5340ace65d02c9aad3f272db5fac2f", "milestone": "M5e-restore-privacy", "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "application_jar_sha256": hashlib.sha256((root / "target/financeclaw-java-0.1.0-SNAPSHOT.jar").read_bytes()).hexdigest(),
            "toolchain": json.loads((root / "toolchains.lock.json").read_text()),
            "environment": json.loads((root / "target/m0/environment.json").read_text()),
            "memory_environment": json.loads((root / "target/m4/environment.json").read_text()),
            "memory_worker_process": json.loads((root / "target/m4/process.json").read_text()),
            "ziwei_dates_environment": json.loads((root / "target/m6/dates-environment.json").read_text()),
            "ziwei_calendar_environment": json.loads((root / "target/m6/calendar-results.json").read_text()),
            "ziwei_chart_environment": json.loads((root / "target/m6/chart-results.json").read_text()),
            "ziwei_domain_baseline": json.loads((root / "src/test/resources/ziwei/domain/manifest.json").read_text()),
            "ziwei_chart_packaging": json.loads((root / "target/m6/chart-packaging.json").read_text()),
            "ziwei_runtime_packaging": packaging,
            "live_validator_contract_tests": validator,
            "drain_preflight_contract_tests": preflight,
            "restore_privacy_contract_tests": privacy,
            "total_tests": sum(s["tests"] for s in suites), "suites": suites, "source_sha256": manifest,
            "validation_scope": "Local PostgreSQL, pgvector and independent JVMs; native loopback model/MCP/Feishu adapters; M6 date contracts, calendar compatibility, 2524 full chart comparisons, domain facts and focus parity, native Toolkit suspension/resume, scoped artifacts, HMAC, budgets and privacy. M6d packaged process crash recovery, cancellation/privacy competition, deployment image with five native tools and no Python, offline live-validator contracts. Development gate retained: no real model Ziwei selection, real provider/MCP/Feishu tenant or remote CI execution. Compose validation is recorded separately."}
evidence['validation_scope'] += ' M5b owner-scoped admission pause/resume, revision and idempotency receipts, PostgreSQL submission/pause races, existing approval/input/cancellation and Feishu continuity, drain counts including uncertain effects, notification and memory backlogs.'
evidence['validation_scope'] += ' M5c read-only owner-scoped keyset pages for task, notification, unprojected binding and memory job blockers; terminal-turn residual facts, numeric job ordering, foreign-owner exclusion and content minimization.'
evidence['validation_scope'] += ' M5d adds only delivery tooling: read-only operations API observations, inventory/count comparison, incomplete scan handling and private report files. The Java build inputs and jar retain their M5c full-suite validation. Preflight fixtures and loopback HTTP/CLI contracts ran separately; deployed CLI observations are in Compose evidence.'
evidence['validation_scope'] += ' M5e adds offline all-owner privacy snapshot comparison and a read-only SQL snapshot query. Java inputs remain unchanged; SQL and pre-forget backup isolation are exercised separately in Compose. Matching supplied snapshots does not prove reference freshness or authorize production recovery.'
destination = root / "target/m5e/results.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
print(f"{evidence['total_tests']} tests passed; evidence: {destination}")
