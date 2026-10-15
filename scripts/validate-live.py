#!/usr/bin/env python3
"""Explicit opt-in read-only model/MCP acceptance through the existing governed Java API."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

parser = argparse.ArgumentParser()
parser.add_argument('--kind', choices=['model', 'mcp'], required=True)
parser.add_argument('--prompt', required=True)
parser.add_argument('--expected-tool', required=True)
a = parser.parse_args()
if os.environ.get('FINANCECLAW_LIVE_ACCEPTANCE') != 'true':
    raise SystemExit('Set FINANCECLAW_LIVE_ACCEPTANCE=true only for the intended test environment.')
base = os.environ.get('FINANCECLAW_LIVE_BASE_URL', '').rstrip('/')
token = os.environ.get('FINANCECLAW_LIVE_API_TOKEN', '')
uri = urlsplit(base)
if (not token or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment
        or uri.scheme not in ('http', 'https')
        or (uri.scheme != 'https' and uri.hostname not in ('127.0.0.1', 'localhost'))):
    raise SystemExit('An HTTPS test endpoint (or loopback) and API token are required.')
if a.kind == 'model' and a.expected_tool != 'search_catalog':
    raise SystemExit('The model acceptance probe must use the read-only search_catalog tool.')
if a.kind == 'mcp' and a.expected_tool not in ('searchHotels', 'getHotelDetail', 'getHotelSearchTags'):
    raise SystemExit('Select one of the enabled read-only hotel MCP tools for MCP acceptance.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect())


def call(path, body=None, method=None, raw=False):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 method=method, headers={'Authorization': 'Bearer ' + token,
                                     'Idempotency-Key': str(uuid.uuid4()), 'Content-Type': 'application/json'})
    with opener.open(req, timeout=40) as response:
        data = response.read()
        return data.decode() if raw else json.loads(data)


start = time.monotonic()
c = call('/v1/conversations', {})['conversation_id']
grant = call(f'/v1/conversations/{c}/grant')
call(f'/v1/conversations/{c}/grant', {'revision': grant['revision'],
     'scopes': ['catalog:read', 'artifact:read', 'travel:read']}, method='PUT')
r = call(f'/v1/conversations/{c}/turns', {'message': a.prompt})
p = f'/v1/conversations/{c}/turns/{r["turn_id"]}'
until = time.monotonic() + 120
while True:
    t = call(p)
    if t['status'] in ('completed', 'failed', 'blocked', 'cancelled', 'waiting'):
        break
    if time.monotonic() > until:
        raise SystemExit('Live test timed out; inspect the original Turn, do not resubmit with a new key.')
    time.sleep(1)
if t['status'] != 'completed':
    raise SystemExit('Live acceptance did not complete; inspect the original Turn in the test environment.')
stream = call(p + '/events', raw=True)
events = [json.loads(line[5:].strip()) for line in stream.splitlines() if line.startswith('data:')]
if not any(e.get('kind') == 'tool_completed' and e.get('tool') == a.expected_tool for e in events):
    raise SystemExit('No successful expected tool event; a text-only/model guess is not passing evidence.')
out = Path('target/m5')
out.mkdir(parents=True, exist_ok=True)
(out / f'live-{a.kind}.json').write_text(json.dumps({
    'kind': a.kind, 'validated_at': datetime.now(timezone.utc).isoformat(),
    'turn_id': r['turn_id'], 'conversation_id': c, 'tool': a.expected_tool,
    'elapsed_seconds': time.monotonic()-start,
    'prompt_sha256': hashlib.sha256(a.prompt.encode()).hexdigest(),
    'status': 'passed', 'scope': 'tool event and completed Turn; not answer quality or Feishu delivery'
}, indent=2)+'\n')
print(f'Live {a.kind} tool acceptance passed; redacted evidence saved in target/m5/.')
