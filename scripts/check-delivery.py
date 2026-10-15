#!/usr/bin/env python3
"""Static documentation, Python/shell syntax and credential-pattern checks; no network calls."""
import ast
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

root = Path(__file__).resolve().parents[1]
for script in (root / 'scripts').glob('*.py'):
    ast.parse(script.read_text(), filename=str(script))
for folder in ('scripts', 'ops'):
    for script in (root / folder).glob('*.sh'):
        subprocess.run(['bash', '-n', str(script)], check=True)
for file in [root / 'README.md', *(root / 'docs').rglob('*.md')]:
    for target in re.findall(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', file.read_text()):
        target = target.strip('<>')
        if target.startswith(('https:', 'http:', '#', 'mailto:')):
            continue
        path = unquote(target.split('#')[0])
        if path and not (file.parent / path).exists():
            raise SystemExit(f'Missing local link in {file.relative_to(root)}: {path}')
paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard'], cwd=root, text=True).splitlines()
pattern = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{24,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bAKIA[0-9A-Z]{16}\b')
for name in set(paths):
    file = root / name
    if not file.is_file():
        continue
    try:
        text = file.read_text()
    except UnicodeDecodeError:
        continue
    if pattern.search(text):
        raise SystemExit(f'Credential-like value in {name}; value omitted')
subprocess.run(['git', 'diff', '--check'], cwd=root, check=True)
print('Static delivery checks passed (syntax, local links, credential patterns, whitespace).')
