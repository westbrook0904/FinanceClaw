"""Fail CI when committed application surfaces contain recognizable live credentials."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("financeclaw", "tests", "scripts", ".github", "config", "docs", "evals")
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "JWT bearer token": re.compile(r"(?i)bearer\s+eyJ[A-Za-z0-9_-]{20,}"),
}


def main() -> None:
    findings: list[str] = []
    for relative in SCAN_ROOTS:
        root = ROOT / relative
        if not root.exists():
            continue
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
            if "__pycache__" in path.parts:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for name, pattern in PATTERNS.items():
                for match in pattern.finditer(content):
                    line = content.count("\n", 0, match.start()) + 1
                    findings.append(f"{path.relative_to(ROOT)}:{line}: possible {name}")
    if findings:
        raise SystemExit("\n".join(findings))
    print("No recognizable credential material found in application surfaces")


if __name__ == "__main__":
    main()
