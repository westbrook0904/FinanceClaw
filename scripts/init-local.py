#!/usr/bin/env python3
"""Create local-only credentials without printing or replacing existing secrets."""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
directory = root / '.secrets'
directory.mkdir(mode=0o700, exist_ok=True)
for name in ('db-admin', 'db-migrator', 'db-app', 'db-memory', 'api-token',
             'model-key', 'memory-model-key', 'embedding-key', 'ziwei-key', 'hotel-token', 'taibu-token',
             'feishu-secret', 'feishu-token', 'feishu-encrypt-key'):
    path = directory / name
    if path.exists():
        continue
    value = secrets.token_urlsafe(36) if name.startswith('db-') or name in ('api-token', 'ziwei-key') else ''
    with path.open('x') as stream:
        stream.write(value)
    # Compose file secrets preserve host permissions; the containing directory stays private.
    os.chmod(path, 0o444)
if not (root / '.env').exists():
    (root / '.env').write_text((root / '.env.example').read_text())
print('Local configuration ready. Existing files preserved; no secret values printed.')
