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
for kind, directory in [("unit", "surefire-reports"), ("postgres-integration", "failsafe-reports")]:
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
        or int(summary.findtext("completed", "0")) != sum(s["tests"] for s in suites if s["kind"] == "postgres-integration")):
    raise SystemExit("Failsafe did not report a successful run")
paths = sorted([root / "pom.xml", root / "toolchains.lock.json", root / "mvnw",
                *root.glob(".mvn/**/*properties"), *root.glob(".mvn/**/*.xml"), *root.glob("src/**/*.java"),
                *root.glob("src/**/*.sql"), *root.glob("src/**/*.yml"),
                *root.glob("scripts/*.py"), *root.glob("scripts/*.sh"), *root.glob(".github/workflows/*.yml")])
# This collector is executed after the tests; it is fingerprinted but is not a Java test input.
if any(p.stat().st_mtime > min(r.stat().st_mtime for r in report_paths)
       for p in paths if p != Path(__file__).resolve()):
    raise SystemExit("Sources changed after tests started; run verify again before collecting evidence")
manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
evidence = {"base_commit": "05b2b488d23ce2312a6d94524c9d43208f75efdd", "milestone": "M2", "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "toolchain": json.loads((root / "toolchains.lock.json").read_text()),
            "environment": json.loads((root / "target/m0/environment.json").read_text()),
            "total_tests": sum(s["tests"] for s in suites), "suites": suites, "source_sha256": manifest,
            "validation_scope": "Local real PostgreSQL and independent JVMs; deterministic model, SDK loopback HTTP provider and mock MCP transport; no real model/MCP/Feishu or CI execution"}
destination = root / "target/m2/results.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
print(f"{evidence['total_tests']} tests passed; evidence: {destination}")
