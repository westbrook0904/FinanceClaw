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
evidence = {"base_commit": "a2b67b1fcb9908892a722d220fad9d469d6fc46d", "milestone": "M6b-charts", "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "application_jar_sha256": hashlib.sha256((root / "target/financeclaw-java-0.1.0-SNAPSHOT.jar").read_bytes()).hexdigest(),
            "toolchain": json.loads((root / "toolchains.lock.json").read_text()),
            "environment": json.loads((root / "target/m0/environment.json").read_text()),
            "memory_environment": json.loads((root / "target/m4/environment.json").read_text()),
            "memory_worker_process": json.loads((root / "target/m4/process.json").read_text()),
            "ziwei_dates_environment": json.loads((root / "target/m6/dates-environment.json").read_text()),
            "ziwei_calendar_environment": json.loads((root / "target/m6/calendar-results.json").read_text()),
            "ziwei_chart_environment": json.loads((root / "target/m6/chart-results.json").read_text()),
            "ziwei_chart_packaging": json.loads((root / "target/m6/chart-packaging.json").read_text()),
            "total_tests": sum(s["tests"] for s in suites), "suites": suites, "source_sha256": manifest,
            "validation_scope": "Local PostgreSQL, pgvector and independent JVMs; native loopback model/MCP/Feishu adapters; M6 date contracts, exhaustive 1901-2099 calendar compatibility, selected timezone offsets, 2524 complete fixed-rule Java chart comparisons and isolated packaged JVMs. No Ziwei product governance or tool enablement, real provider/MCP/Feishu tenant or remote CI execution"}
destination = root / "target/m6/results.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
print(f"{evidence['total_tests']} tests passed; evidence: {destination}")
