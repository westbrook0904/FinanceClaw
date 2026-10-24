#!/usr/bin/env python3
"""Combined-runtime delivery/failure/backup experiment with optional governance checks."""
import concurrent.futures
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--ziwei', action='store_true', help='Also boot the shared Ziwei development policy and secret configuration')
parser.add_argument('--admission', action='store_true', help='Verify durable admission pause, drain and resume across deployment recovery')
parser.add_argument('--preflight', action='store_true', help='Verify the read-only drain report CLI (requires --admission)')
parser.add_argument('--restore-privacy', action='store_true', help='Keep pre-forget backups isolated and compare all-owner privacy (requires --preflight)')
ARGS = parser.parse_args()
if ARGS.preflight and not ARGS.admission:
    parser.error('--preflight requires --admission')
if ARGS.restore_privacy and not ARGS.preflight:
    parser.error('--restore-privacy requires --preflight')
OUT = ROOT / ('target/m5e/deployment' if ARGS.restore_privacy else 'target/m5d/deployment' if ARGS.preflight else 'target/m5b/deployment' if ARGS.admission else 'target/m6/deployment' if ARGS.ziwei else 'target/m5')
OUT.mkdir(parents=True, exist_ok=True)
THRESHOLDS = json.loads((ROOT / 'ops/m5-thresholds.json').read_text())
PROJECT = ('financeclaw-m5e-' if ARGS.restore_privacy else 'financeclaw-m5d-' if ARGS.preflight else 'financeclaw-m5b-' if ARGS.admission else 'financeclaw-m6d-' if ARGS.ziwei else 'financeclaw-m5-') + uuid.uuid4().hex[:10]
TEMP = ROOT / '.cache' / PROJECT
TEMP.mkdir(parents=True)
ENV = os.environ.copy()
# This experiment cannot inherit a configured live provider, tenant or Feishu channel.
ENV.update({k: '' for k in ENV if k.startswith('FINANCECLAW_')})
ENV.update(FINANCECLAW_HTTP_PORT='0', FINANCECLAW_MODEL='offline',
           FINANCECLAW_ZIWEI_ENABLED='false', FINANCECLAW_ZIWEI_DEVELOPMENT='false',
           FINANCECLAW_HOTEL_ENABLED='false', FINANCECLAW_TAIBU_ENABLED='false',
           FINANCECLAW_FEISHU_ENABLED='false', FINANCECLAW_MEMORY_ENABLED='true',
           FINANCECLAW_PROFILE_ENABLED='true',
           FINANCECLAW_TENANT='local', FINANCECLAW_SUBJECT='developer',
           FINANCECLAW_EMBEDDING_REVISION='m5-no-embedding-v1')
if ARGS.ziwei:
    ENV.update(FINANCECLAW_ZIWEI_ENABLED='true', FINANCECLAW_ZIWEI_DEVELOPMENT='true',
               FINANCECLAW_ZIWEI_KEY_VERSION='m6d-deployment-v1',
               FINANCECLAW_ZIWEI_QUERY_TIMEZONE='Asia/Shanghai')
# --env-file avoids silently loading real endpoints from the operator's .env.
(TEMP / 'empty.env').write_text('')
market = {'AAPL': {'price': '100', 'currency': 'USD', 'provider': 'm5-fixture',
                   'as_of': datetime.now(timezone.utc).isoformat()}}
(TEMP / 'market.json').write_text(json.dumps(market))
AGENT_NAME = 'P2a synthetic agent'
AGENT_PROMPTS = ('P2a synthetic prompt revision one.', 'P2a synthetic prompt revision two.')
AGENT_FILES = []
for index, prompt in enumerate(AGENT_PROMPTS, 1):
    definition = TEMP / f'agent-definition-{index}.json'
    definition.write_text(json.dumps({'displayName': AGENT_NAME, 'systemPrompt': prompt}), encoding='utf-8')
    # Synthetic fixture only; readable by the container user and mounted read-only per command.
    definition.chmod(0o644)
    AGENT_FILES.append(definition)
for index, capabilities in enumerate(([], ['function:search_catalog']), 3):
    definition = TEMP / f'agent-definition-{index}.json'
    definition.write_text(json.dumps({'displayName': AGENT_NAME, 'systemPrompt': AGENT_PROMPTS[0],
                                     'composition': {'modelProfile': 'primary', 'capabilities': capabilities}}), encoding='utf-8')
    definition.chmod(0o644)
    AGENT_FILES.append(definition)
override = {'services': {s: {
    'environment': {'FINANCECLAW_MARKET_FILE': '/app/market.json'},
    'volumes': [f'{TEMP / "market.json"}:/app/market.json:ro']}
    for s in ('api',)}}
(TEMP / 'override.json').write_text(json.dumps(override))
BASE = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(TEMP / 'empty.env'),
        '-p', PROJECT, '-f', str(ROOT / 'compose.yml'), '-f', str(TEMP / 'override.json')]
LOG = (OUT / 'compose-validation.log').open('w')
RESULT = {'scope': 'isolated real containers; offline model and synthetic market only',
          'ziwei_development_policy': ARGS.ziwei,
          'admission_control': ARGS.admission,
          'read_only_preflight': ARGS.preflight,
          'restore_privacy': ARGS.restore_privacy,
          'project': PROJECT, 'thresholds': THRESHOLDS, 'checks': {},
          'started_at': datetime.now(timezone.utc).isoformat()}
INPUTS = ('Dockerfile', '.dockerignore', 'compose.yml', 'ops/postgres-init.sql',
          'ops/container-entrypoint.sh', 'src/main/resources/application-standalone.yml',
          'ops/m5-thresholds.json', 'scripts/validate-compose.py')
if ARGS.preflight:
    INPUTS += ('scripts/inspect-drain.py',)
if ARGS.restore_privacy:
    INPUTS += ('scripts/check-restore-privacy.py', 'ops/restore-privacy-snapshot.sql')
RESULT['source_sha256'] = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in INPUTS}
(OUT / 'compose-results.json').unlink(missing_ok=True)
URL = ''
TOKEN = (ROOT / '.secrets/api-token').read_text()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROFILE_TEXT = '# User profile\n\n- Preferred response: concise.\n- Marker: m5-profile-private-sentinel.\n'


def command(args, *, data=None, timeout=240, expected_exit=0):
    result = subprocess.run(args, cwd=ROOT, env=ENV, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    LOG.write(result.stderr.decode(errors='replace'))
    LOG.flush()
    if result.returncode != expected_exit:
        raise RuntimeError(f'Command failed (exit {result.returncode}); see {OUT}/compose-validation.log')
    return result.stdout


def compose(*args, **kw):
    return command(BASE + list(args), **kw)


def agent_admin(action, agent_id=None, *options, definition=None, subject='developer', expected_exit=0):
    arguments = ['run', '--rm', '--no-deps']
    if definition is not None:
        assert definition in AGENT_FILES
        arguments.extend(['-v', f'{definition}:/input/agent-definition.json:ro'])
    arguments.extend(['api', 'agent-admin', action, '--tenant', 'local', '--subject', subject])
    if agent_id is not None:
        arguments.extend(['--agent-id', agent_id])
    if definition is not None:
        arguments.extend(['--file', '/input/agent-definition.json'])
    arguments.extend(options)
    raw = compose(*arguments, expected_exit=expected_exit)
    assert len(raw.strip().splitlines()) == 1
    text = raw.decode('utf-8')
    assert all(value not in text for value in (*AGENT_PROMPTS, AGENT_NAME, TOKEN,
                                              '"systemPrompt"', '"displayName"', '"tenant"', '"subject"'))
    response = json.loads(raw)
    if expected_exit == 2:
        assert set(response) == {'result', 'error'} and response['result'] == 'blocked'
    return response


def restart_runtime(*, execute=True):
    """Exercise queued recovery without adding a second application process."""
    global URL
    override['services']['api']['command'] = ['standalone']
    if not execute:
        override['services']['api']['command'].append('--financeclaw.worker.enabled=false')
    (TEMP / 'override.json').write_text(json.dumps(override))
    compose('up', '-d', '--no-deps', '--wait', 'api')
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)


def sql(statement, database='financeclaw_java'):
    return compose('exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-d', database,
                   '-v', 'ON_ERROR_STOP=1', '-tA', '-c', statement).decode().strip()


def request(path, body=None, key=None, method=None, auth=True):
    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Authorization'] = 'Bearer ' + TOKEN
    if key:
        headers['Idempotency-Key'] = key
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=12) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0, None


def api(path, body=None, key=None, method=None):
    status, result = request(path, body, key, method)
    assert status in (200, 201, 202), (path, status)
    return result


def profile(markdown=None, version=None):
    headers = {'Authorization': 'Bearer ' + TOKEN}
    if markdown is not None:
        headers.update({'Content-Type': 'text/markdown; charset=utf-8', 'If-Match': version})
    req = urllib.request.Request(URL + '/v1/memory/profile.md',
                                 data=None if markdown is None else markdown.encode('utf-8'),
                                 method='GET' if markdown is None else 'PUT', headers=headers)
    try:
        with OPENER.open(req, timeout=12) as response:
            assert response.headers.get('Cache-Control') == 'no-store'
            assert response.headers.get_content_type() == 'text/markdown'
            return response.status, response.read().decode('utf-8'), response.headers['ETag']
    except urllib.error.HTTPError as error:
        return error.code, None, None


def wait(check, seconds=45):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.2)
    raise AssertionError('Timed out waiting for expected state')


def mark(name, details=True):
    RESULT['checks'][name] = details
    print('PASS:', name, flush=True)


def turn(message='M5 synthetic request', agent_id=None):
    c = api('/v1/conversations', {} if agent_id is None else {'agent_id': agent_id})['conversation_id']
    receipt = api(f'/v1/conversations/{c}/turns', {'message': message}, 'admit-once')
    path = f'/v1/conversations/{c}/turns/{receipt["turn_id"]}'
    return c, receipt, path


def terminal(path, status='completed'):
    return wait(lambda: (value if (value := api(path))['status'] == status else None),
                THRESHOLDS['recovery_seconds'])


def fingerprints(database):
    tables = sql("SELECT schemaname||'.'||tablename FROM pg_tables WHERE schemaname IN "
                 "('business','agent_runtime','memory','channel','control_plane') ORDER BY 1", database).splitlines()
    assert tables and all(re.fullmatch(r'[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*', t) for t in tables)
    # One statement preserves the original per-table count/hash formula and shares one snapshot.
    # Avoid hundreds of Docker/psql invocations when comparing several isolated restore databases.
    queries = [f"SELECT '{table}' AS name, count(*)||':'||md5(COALESCE("
               f"string_agg(md5(to_jsonb(t)::text),'' ORDER BY md5(to_jsonb(t)::text)),'')) AS fingerprint FROM {table} t"
               for table in tables]
    return json.loads(sql('SELECT json_object_agg(name,fingerprint) FROM (' + ' UNION ALL '.join(queries) + ') hashes', database))


def admission(paused, key):
    state = api('/v1/operations/admission')
    return api('/v1/operations/admission', {'paused': paused, 'revision': state['revision']}, key, 'PUT')


def rejected_submission(database='financeclaw_java'):
    before = sql('SELECT count(*) FROM business.turns', database)
    conversation = api('/v1/conversations', {})['conversation_id']
    assert request(f'/v1/conversations/{conversation}/turns', {'message': 'paused submission'}, 'paused')[0] == 503
    assert sql('SELECT count(*) FROM business.turns', database) == before


def preflight(name, expected, page_size=2, max_pages=20):
    destination = OUT / 'preflight' / PROJECT / (name + '.json')
    command([sys.executable, str(ROOT / 'scripts/inspect-drain.py'), '--base-url', URL,
             '--token-file', str(ROOT / '.secrets/api-token'), '--output', str(destination),
             '--page-size', str(page_size), '--max-pages', str(max_pages)],
            expected_exit={'ready_for_review': 0, 'blocked': 2, 'incomplete': 3}[expected])
    report = json.loads(destination.read_text())
    assert report['assessment'] == expected and report['scope'] == 'current_owner'
    assert report['source_sha256'] == RESULT['source_sha256']['scripts/inspect-drain.py']
    assert TOKEN not in destination.read_text()
    assert destination.stat().st_mode & 0o777 == 0o600
    mark('preflight_' + name, {'assessment': expected, 'errors': report['errors'],
         'observations_match': report.get('observations_match'),
         'inventory_matches_counts': report.get('inventory_matches_counts'),
         'items': {kind: len(rows) for kind, rows in report['items'].items()},
         'report_sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})


def privacy_snapshot(database, name):
    # No tenant filtering. Capture inside one read-only repeatable-read transaction.
    raw = compose('exec', '-T', 'postgres', 'psql', '-X', '-qAt', '-U', 'postgres',
                  '-d', database, '-v', 'ON_ERROR_STOP=1',
                  data=(ROOT / 'ops/restore-privacy-snapshot.sql').read_bytes())
    assert json.loads(raw)['schema_version'] == 3
    assert PROFILE_TEXT.encode('utf-8') not in raw and b'm5-profile-private-sentinel' not in raw
    destination = TEMP / (name + '.json')
    with destination.open('xb') as out:
        os.chmod(destination, 0o600)
        out.write(raw)
    return destination


def privacy_check(reference, restored_snapshot, name, expected):
    destination = OUT / 'privacy' / PROJECT / (name + '.json')
    command([sys.executable, str(ROOT / 'scripts/check-restore-privacy.py'),
             '--reference', str(reference), '--restored', str(restored_snapshot), '--output', str(destination)],
            expected_exit={'privacy_matches_reference': 0, 'blocked': 2, 'incomplete': 3}[expected])
    report = json.loads(destination.read_text())
    assert report['assessment'] == expected and report['schema_version'] == 3
    assert destination.stat().st_mode & 0o777 == 0o600
    for path, label in ((reference, 'reference'), (restored_snapshot, 'restored')):
        assert report['input_sha256'][label] == hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('check-restore-privacy.py', 'inspect-drain.py'):
        assert report['source_sha256'][name] == RESULT['source_sha256']['scripts/' + name]
    assert not any(value in destination.read_text()
                   for value in (TOKEN, 'response_style', '简洁', 'm5-profile-private-sentinel'))
    return report


def require_no_runtime_connections(*databases):
    # Only the PostgreSQL container is running. The check is an observation, not a global fence.
    running = compose('ps', '--services', '--status', 'running').decode().split()
    assert running == ['postgres'], running
    for database in databases:
        assert sql(f"SELECT count(*) FROM pg_stat_activity WHERE datname='{database}'", 'postgres') == '0'


try:
    print('Starting isolated deployment validation:', PROJECT, flush=True)
    compose('config', '--quiet')
    config = json.loads(compose('config', '--format', 'json'))
    assert set(config['services']) == {'postgres', 'init-db', 'api', 'memory-worker'}
    assert config['services']['api']['command'] == ['standalone']
    runtime_secrets = {s['source'] for s in config['services']['api']['secrets']}
    assert {'api_token', 'model_key', 'hotel_token', 'taibu_token', 'feishu_secret'} <= runtime_secrets
    mark('single_combined_runtime_and_restricted_memory_topology')
    if ARGS.ziwei:
        for role in ('api',):
            service = config['services'][role]
            assert service['environment']['FINANCECLAW_ZIWEI_ENABLED'] == 'true'
            assert service['environment']['FINANCECLAW_ZIWEI_DEVELOPMENT'] == 'true'
            assert service['environment']['FINANCECLAW_ZIWEI_KEY_REF'] == 'ZIWEI_HMAC_KEY'
            assert any(s['source'] == 'ziwei_key' and s['target'] == 'ZIWEI_HMAC_KEY'
                       for s in service['secrets'])
        for role in ('init-db', 'memory-worker'):
            assert not any(s['source'] == 'ziwei_key' for s in config['services'][role]['secrets'])
        mark('shared_ziwei_policy_and_role_scoped_secret_mounts')
    compose('build', 'api')
    started = time.monotonic()
    compose('up', '-d', '--wait', '--wait-timeout', str(THRESHOLDS['startup_seconds']))
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)
    assert request('/v1/operations', auth=False)[0] == 401
    assert sql('SELECT count(*) FROM public.flyway_schema_history WHERE success') == str(
        len(list((ROOT / 'src/main/resources/db/migration').glob('V*__*.sql'))))
    mark('empty_database_and_healthy_combined_runtime', {'startup_seconds': time.monotonic()-started})
    for retired in ('business.skill_refs', 'agent_runtime.tool_effects',
                    'business.outbox', 'business.projections'):
        assert sql("SELECT to_regclass('" + retired + "') IS NULL") == 't', retired
    assert sql("SELECT to_regclass('business.child_invocations') IS NOT NULL") == 't'
    mark('fresh_schema_has_only_current_application_tables')
    assert sql("SELECT has_schema_privilege('financeclaw_memory','business','USAGE')") == 'f'
    assert sql("SELECT has_schema_privilege('financeclaw_memory','control_plane','USAGE,CREATE')") == 'f'
    assert sql("SELECT has_schema_privilege('financeclaw_app','business','CREATE')") == 'f'
    for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
        assert sql("SELECT has_table_privilege('financeclaw_app','business.child_invocations','"
                   + privilege + "')") == 't'
    assert sql("SELECT has_table_privilege('financeclaw_memory','business.child_invocations','SELECT')") == 'f'
    for role in ('memory-worker',):
        code = compose('exec', '-T', role, 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                       'http://localhost:8080/v1/conversations').decode()
        assert code == '404', (role, code)
    mark('background_http_and_database_privilege_boundaries')

    release_before = sql("SELECT release_hash || ':' || revision FROM control_plane.deployments WHERE name='default'")
    history_before = json.loads(compose('run', '--rm', '--no-deps', 'api',
                                       'release-admin', 'history'))
    prepared = json.loads(compose('run', '--rm', '--no-deps', 'api',
                                  'release-admin', 'prepare'))
    assert prepared['result'] == 'prepared'
    assert re.fullmatch('[0-9a-f]{64}', prepared['candidateHash'])
    assert prepared['current']['releaseHash'] + ':' + str(prepared['current']['revision']) == release_before
    assert sql("SELECT release_hash || ':' || revision FROM control_plane.deployments WHERE name='default'") == release_before
    history_after = json.loads(compose('run', '--rm', '--no-deps', 'api',
                                      'release-admin', 'history'))
    assert history_before == history_after
    assert history_after['result'] == 'history' and len(history_after['items']) == 1
    mark('release_admin_container_prepares_without_overwriting_default_or_history')

    agent_id, foreign_agent_id = str(uuid.uuid4()), str(uuid.uuid4())
    created = agent_admin('create', agent_id, definition=AGENT_FILES[0])
    assert created['result'] == 'created' and created['draftRevision'] == 1
    assert created['baseReleaseHash'] == prepared['current']['releaseHash']
    assert created['inputSha256'] == hashlib.sha256(AGENT_FILES[0].read_bytes()).hexdigest()
    assert agent_admin('create', agent_id, definition=AGENT_FILES[0]) == created
    assert agent_admin('get', agent_id)['contentHash'] == created['contentHash']
    agent_admin('create', foreign_agent_id, definition=AGENT_FILES[0], subject='p2a-other-owner')
    assert agent_admin('get', foreign_agent_id, expected_exit=2)['error'] == 'not_found'
    assert request('/v1/agents', auth=False)[0] == 401
    visible_agents = api('/v1/agents')
    assert all(set(item) == {'agent_id', 'draft_revision', 'builtin'} for item in visible_agents)
    assert {item['agent_id'] for item in visible_agents} == {'finance_agent', agent_id}
    assert request('/v1/conversations', {'agent_id': agent_id})[0] == 409
    assert request('/v1/conversations', {'agent_id': foreign_agent_id})[0] == 404
    first_publish = agent_admin('publish', agent_id, '--draft-revision', '1', '--expected-revision', '0')
    assert first_publish['result'] == 'published' and first_publish['revision'] == 1
    agent_release_one = first_publish['releaseHash']
    old_agent_conversation, old_agent_receipt, old_agent_path = turn('P2a first published request', agent_id)
    terminal(old_agent_path)
    assert sql(f"SELECT agent_id||':'||release_hash FROM business.conversations WHERE id='{old_agent_conversation}'") == agent_id + ':' + agent_release_one
    mark('agent_admin_create_owner_isolation_and_targeted_http_execution')

    updated = agent_admin('update', agent_id, '--expected-revision', '1', definition=AGENT_FILES[1])
    assert updated['draftRevision'] == 2 and updated['contentHash'] != created['contentHash']
    assert updated['baseReleaseHash'] == created['baseReleaseHash']
    assert agent_admin('update', agent_id, '--expected-revision', '1', definition=AGENT_FILES[1],
                       expected_exit=2)['error'] == 'agent_draft_revision_changed'
    history_one = agent_admin('history', agent_id)
    assert len(history_one['items']) == 1 and history_one['items'][0]['releaseHash'] == agent_release_one
    assert agent_admin('publish', agent_id, '--draft-revision', '1', '--expected-revision', '1',
                       expected_exit=2)['error'] == 'agent_draft_revision_changed'
    second_publish = agent_admin('publish', agent_id, '--draft-revision', '2', '--expected-revision', '1')
    agent_release_two = second_publish['releaseHash']
    assert second_publish['revision'] == 2 and agent_release_two != agent_release_one
    assert agent_admin('publish', agent_id, '--draft-revision', '2', '--expected-revision', '1',
                       expected_exit=2)['error'] == 'release_revision_changed'
    history_two = agent_admin('history', agent_id, '--after-revision', '1', '--limit', '1')
    assert history_two['nextAfterRevision'] == 2 and len(history_two['items']) == 1
    assert history_two['items'][0]['previousHash'] == agent_release_one
    assert history_two['items'][0]['releaseHash'] == agent_release_two
    old_agent_next = api(f'/v1/conversations/{old_agent_conversation}/turns',
                         {'message': 'P2a old conversation keeps first release'}, 'p2a-old-after-publish')
    terminal(f'/v1/conversations/{old_agent_conversation}/turns/{old_agent_next["turn_id"]}')
    assert sql(f"SELECT release_hash FROM business.turns WHERE id='{old_agent_next['turn_id']}'") == agent_release_one
    new_agent_conversation, new_agent_receipt, new_agent_path = turn('/workflow portfolio_review P2a plain user input', agent_id)
    terminal(new_agent_path)
    assert sql(f"SELECT agent_id||':'||release_hash FROM business.turns WHERE id='{new_agent_receipt['turn_id']}'") == agent_id + ':' + agent_release_two
    assert sql("SELECT count(*) FROM business.capability_steps WHERE step='publish_report'") == '0'
    assert sql("SELECT release_hash || ':' || revision FROM control_plane.deployments WHERE name='default'") == release_before
    mark('agent_draft_publish_cas_pins_existing_sessions_and_keeps_builtin_driver_separate')

    composed_agent = str(uuid.uuid4())
    agent_admin('create', composed_agent, definition=AGENT_FILES[2])
    catalog = agent_admin('catalog', composed_agent)['catalog']
    assert 'primary' in catalog['modelProfiles']
    assert 'function:search_catalog' in {item['id'] for item in catalog['capabilities']}
    empty_release = agent_admin('publish', composed_agent, '--draft-revision', '1', '--expected-revision', '0')['releaseHash']
    manifest = json.loads(sql(f"SELECT manifest FROM control_plane.release_manifests WHERE release_hash='{empty_release}'"))
    assert manifest['format'] == 'agent-release/3' and manifest['plan']['capabilities'] == []
    assert manifest['plan']['definition']['scopes'] == []
    pure_conversation, pure_receipt, pure_path = turn('P2b pure conversation', composed_agent)
    terminal(pure_path)
    mark('composed_agent_catalog_and_explicit_empty_plan_execute_in_packaged_runtime')
    agent_admin('update', composed_agent, '--expected-revision', '1', definition=AGENT_FILES[3])
    function_release = agent_admin('publish', composed_agent, '--draft-revision', '2', '--expected-revision', '1')['releaseHash']
    selected_manifest = json.loads(sql(f"SELECT manifest FROM control_plane.release_manifests WHERE release_hash='{function_release}'"))
    assert {item['id'] for item in selected_manifest['plan']['capabilities']} == {'function:search_catalog', 'function:read_artifact_range'}
    assert selected_manifest['plan']['runtimeArtifactHash'] == manifest['plan']['runtimeArtifactHash']
    terminal(turn('P2b selected functions', composed_agent)[2])
    pinned = api(f'/v1/conversations/{pure_conversation}/turns', {'message': 'P2b prior session'}, 'p2b-pinned')
    terminal(f'/v1/conversations/{pure_conversation}/turns/{pinned["turn_id"]}')
    assert sql(f"SELECT release_hash FROM business.turns WHERE id='{pinned['turn_id']}'") == empty_release
    mark('composed_agent_dependencies_are_frozen_and_prior_session_keeps_empty_plan')

    def sample(_):
        start = time.monotonic()
        c, receipt, path = turn()
        replay = api(f'/v1/conversations/{c}/turns', {'message': 'M5 synthetic request'}, 'admit-once')
        assert replay['turn_id'] == receipt['turn_id']
        terminal(path)
        return time.monotonic()-start
    with concurrent.futures.ThreadPoolExecutor(THRESHOLDS['concurrency']) as pool:
        durations = list(pool.map(sample, range(THRESHOLDS['requests'])))
    p95 = sorted(durations)[math.ceil(.95*len(durations))-1]
    assert p95 < THRESHOLDS['p95_turn_seconds'], p95
    mark('offline_load_and_idempotency', {'requests': len(durations), 'success_ratio': 1,
                                        'p95_seconds': p95, 'provider_calls': 0, 'provider_cost': 0})

    restart_runtime(execute=False)
    c, receipt, path = turn('durable admission across API restart')
    assert api(path)['status'] == 'queued'
    if ARGS.admission:
        pause = admission(True, 'pause-before-restart')
        report = api('/v1/operations/drain')
        assert report['scope'] == 'current_owner' and not report['drained']
        assert report['counts']['queued'] == 1 and report['counts']['active_commands'] == 1
        if ARGS.preflight:
            preflight('queued_and_memory_backlog', 'blocked')
            preflight('truncated_inventory', 'incomplete', page_size=1, max_pages=1)
    compose('restart', 'api')
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)
    assert agent_admin('get', agent_id)['contentHash'] == updated['contentHash']
    assert agent_admin('history', agent_id)['nextAfterRevision'] == 2
    assert sql(f"SELECT count(*) FROM control_plane.release_manifests WHERE release_hash IN ('{agent_release_one}','{agent_release_two}')") == '2'
    assert sql(f"SELECT release_hash FROM business.conversations WHERE id='{old_agent_conversation}'") == agent_release_one
    mark('agent_catalog_and_pinned_conversation_survive_runtime_restart')
    replay = api(f'/v1/conversations/{c}/turns', {'message': 'durable admission across API restart'}, 'admit-once')
    assert replay['turn_id'] == receipt['turn_id']
    if ARGS.admission:
        assert api('/v1/operations/admission') == {'paused': True, 'revision': pause['revision']}
        rejected_submission()
    restart_runtime()
    terminal(path)
    mark('combined_restart_keeps_admission_and_resumes_disabled_execution')
    if ARGS.admission:
        report = api('/v1/operations/drain')
        assert report['counts']['queued'] == 0 and report['counts']['active_commands'] == 0
        # Ordinary offline turns have no configured extractor; expose the backlog, do not hide it.
        assert report['counts']['pending_memory_jobs'] > 0 and not report['drained']
        admission(False, 'resume-after-restart')
        mark('durable_pause_rejects_new_turns_preserves_replay_and_drains_existing_work', report)

    workflow = '/workflow portfolio_review ' + json.dumps({'portfolio_name': 'M5 synthetic',
        'positions': [{'symbol': 'AAPL', 'quantity': '1', 'cost_basis': '90'}]})
    wc, wr, wp = turn(workflow)
    terminal(wp, 'waiting')
    question = api(wp + '/interactions')[0]
    assert question['kind'] == 'approval'
    if ARGS.admission:
        admission(True, 'pause-before-approval')
        report = api('/v1/operations/drain')
        assert not report['drained'] and report['counts']['waiting'] == 1
        assert report['counts']['pending_interactions'] == 1
    compose('kill', '-s', 'SIGKILL', 'api')
    restart_runtime()
    answer = {'kind': 'approval', 'revision': question['revision'], 'decision': 'approve',
              'action_hash': question['action_hash'], 'answer': None}
    api(wp + '/interactions/' + question['id'] + '/answer', answer, 'approval-once')
    terminal(wp)
    api(wp + '/interactions/' + question['id'] + '/answer', answer, 'approval-once')
    assert sql("SELECT count(*) FROM business.capability_steps WHERE step='publish_report'") == '1'
    assert sql("SELECT count(*) FROM business.artifacts WHERE required_scope='workflow:run'") == '1'
    mark('sigkill_and_persisted_approval_execute_once')
    if ARGS.admission:
        assert api('/v1/operations/admission')['paused']
        admission(False, 'resume-after-approval')
        mark('paused_admission_allows_persisted_approval_after_runtime_sigkill')

    compose('stop', 'postgres')
    # Existing pooled connections may be invalidated during the first probe. Require an actual
    # HTTP 503 within the bounded fault window, never treat a timeout/disconnect as success.
    down_statuses = []

    def unavailable(path, auth=True):
        status = request(path, auth=auth)[0]
        down_statuses.append(status)
        return status == 503

    wait(lambda: unavailable('/actuator/health/readiness', auth=False), THRESHOLDS['recovery_seconds'])
    wait(lambda: unavailable(wp), THRESHOLDS['recovery_seconds'])
    recovery = time.monotonic()
    compose('start', 'postgres')
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)
    assert api(wp)['status'] == 'completed'
    after = turn('after database recovery')
    terminal(after[2])
    elapsed = time.monotonic()-recovery
    assert elapsed < THRESHOLDS['recovery_seconds']
    mark('database_outage_fails_readiness_then_recovers', {'recovery_seconds': elapsed,
                                                         'unavailable_http_statuses': down_statuses})

    profile_status, empty_profile, original_version = profile()
    assert profile_status == 200 and empty_profile == '' and original_version
    profile_status, saved_profile, saved_version = profile(PROFILE_TEXT, original_version)
    assert profile_status == 200 and saved_profile == PROFILE_TEXT and saved_version != original_version
    assert profile()[1:] == (PROFILE_TEXT, saved_version)
    assert profile('stale profile edit', original_version)[0] == 409
    assert sql("SELECT count(*) FROM memory.user_profiles WHERE revision=1") == '1'
    assert sql("SELECT bool_and(NOT has_table_privilege('financeclaw_memory',"
               "'memory.user_profiles',p)) FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) p") == 't'
    mark('manual_markdown_profile_has_owner_version_and_restricted_worker_permissions',
         {'revision': 1, 'utf8_bytes': len(PROFILE_TEXT.encode('utf-8'))})

    mc, mr, mp = turn('/remember response_style=简洁')
    terminal(mp)
    wait(lambda: any(f['status'] == 'active' for f in api('/v1/memory')))
    epoch = api('/v1/memory/privacy')['epoch']
    if ARGS.restore_privacy:
        compose('stop', 'api', 'memory-worker')
        require_no_runtime_connections('financeclaw_java')
        # A superseded projection at the current epoch is legitimately retained and query-filtered.
        # Privacy checks must not mistake it for resurrection after forgetting.
        sql("INSERT INTO memory.facts(id,source_id,tenant,subject,epoch,key,content,evidence,revision,status) "
            "SELECT id||'-superseded',source_id,tenant,subject,epoch,'home_city','synthetic','synthetic',2,'superseded' "
            "FROM memory.facts WHERE status='active'; "
            "INSERT INTO memory.projections(fact_id,tenant,subject,epoch,revision,model_revision,dimensions,embedding) "
            "SELECT id,tenant,subject,epoch,1,'m5e-superseded',1024,array_fill(0.1::real,ARRAY[1024]) "
            "FROM memory.facts WHERE status='superseded'")
        current_epoch = privacy_snapshot('financeclaw_java', 'current-epoch-superseded')
        current_report = privacy_check(current_epoch, current_epoch, 'current_epoch_superseded', 'privacy_matches_reference')
        mark('current_epoch_superseded_projection_is_not_a_forget_violation', current_report)
        old_backup = compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'postgres', '-d', 'financeclaw_java', '-Fc')
        assert sql("SELECT count(*) FROM memory.facts WHERE status='active' AND content IS NOT NULL") == '1'
        assert sql("SELECT count(*) FROM memory.user_profiles WHERE markdown<>''") == '1'
        mark('pre_forget_backup_captured_with_writers_stopped', {'sha256': hashlib.sha256(old_backup).hexdigest()})
        compose('up', '-d', '--no-deps', '--wait', 'api', 'memory-worker')
        URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    assert api('/v1/memory/forget', {'epoch': epoch}, 'forget-once')['epoch'] == epoch + 1
    wait(lambda: not api('/v1/memory/privacy')['purge_pending'])
    assert api('/v1/memory') == []
    profile_status, forgotten_profile, forgotten_version = profile()
    assert profile_status == 200 and forgotten_profile == ''
    assert forgotten_version.startswith(f'"profile-{epoch + 1}-0-')
    assert sql('SELECT count(*) FROM memory.user_profiles') == '0'
    assert sql('SELECT max(profile_revision) FROM memory.owners') == '0'
    assert profile(PROFILE_TEXT, saved_version)[0] == 409
    mark('forget_deletes_profile_resets_revision_and_rejects_pre_forget_import')
    # SDK 正文必须已擦除并留下 CAS 墓碑；业务报告仍按原业务留存规则保留。
    owner_states = "user_id=agent_runtime.scope_hash(ARRAY['local','developer'])"
    assert int(sql(f'SELECT count(*) FROM agent_runtime.states WHERE {owner_states} AND deleted')) > 0
    assert sql(f'SELECT count(*) FROM agent_runtime.states WHERE {owner_states} AND NOT deleted') == '0'
    assert sql("SELECT count(*) FROM agent_runtime.unsafe_execution_data() "
               "WHERE tenant='local' AND subject='developer'") == '0'
    assert sql("SELECT count(*) FROM business.capability_steps WHERE step='publish_report'") == '0'
    assert sql("SELECT count(*) FROM business.artifacts WHERE required_scope='workflow:run'") == '1'
    mark('forget_erases_execution_copies_and_preserves_cas_tombstones')
    mark('restricted_memory_worker_and_forgetting')
    RESULT['operations'] = api('/v1/operations')
    if ARGS.admission:
        pause = admission(True, 'pause-before-backup')
        report = wait(lambda: (value if (value := api('/v1/operations/drain'))['drained'] else None))
        assert report['scope'] == 'current_owner' and all(value == 0 for value in report['counts'].values())
        mark('owner_drain_requires_pause_and_all_task_channel_memory_counts_zero', report)
        if ARGS.preflight:
            preflight('paused_and_drained', 'ready_for_review')

    compose('stop', 'api', 'memory-worker')
    original = fingerprints('financeclaw_java')
    assert len(original) == 41
    assert {'control_plane.agents', 'control_plane.agent_draft_revisions',
            'business.child_invocations'} <= original.keys()
    # 两个基础隔离用户 Agent，加上组合能力场景新建的第三个 Agent（含两版草稿）。
    assert original['control_plane.agents'].split(':', 1)[0] == '3'
    assert original['control_plane.agent_draft_revisions'].split(':', 1)[0] == '5'
    backup = compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'postgres', '-d', 'financeclaw_java', '-Fc')
    dump = OUT / 'synthetic-restore.dump'
    dump.write_bytes(backup)
    dump.chmod(0o600)
    restored = 'financeclaw_restore_' + uuid.uuid4().hex[:10]
    sql(f'CREATE DATABASE {restored} OWNER financeclaw')
    compose('exec', '-T', 'postgres', 'pg_restore', '-U', 'postgres', '-d', restored, '--exit-on-error', data=backup)
    assert fingerprints(restored) == original
    assert sql('SELECT max(epoch) FROM memory.owners', restored) == str(epoch + 1)
    assert sql("SELECT count(*) FROM memory.facts WHERE content<>''", restored) == '0'
    assert sql('SELECT count(*) FROM memory.user_profiles', restored) == '0'
    mark('consistent_backup_restore_all_tables_and_privacy_epoch',
         {'tables': len(original), 'sha256': hashlib.sha256(backup).hexdigest(), 'rows_and_hashes': original})
    if ARGS.restore_privacy:
        old_database = 'financeclaw_stale_' + uuid.uuid4().hex[:10]
        sql(f'CREATE DATABASE {old_database} OWNER financeclaw')
        compose('exec', '-T', 'postgres', 'pg_restore', '-U', 'postgres', '-d', old_database, '--exit-on-error', data=old_backup)
        require_no_runtime_connections('financeclaw_java', restored, old_database)
        before_privacy = {db: fingerprints(db) for db in ('financeclaw_java', restored, old_database)}
        reference = privacy_snapshot('financeclaw_java', 'authoritative-privacy')
        stale_snapshot = privacy_snapshot(old_database, 'stale-privacy')
        clean_snapshot = privacy_snapshot(restored, 'restored-privacy')
        stale_report = privacy_check(reference, stale_snapshot, 'old_backup', 'blocked')
        assert 'epochs_match' in stale_report['errors'] and 'privacy_events_match' in stale_report['errors']
        assert sql("SELECT count(*) FROM memory.facts WHERE status='active' AND content IS NOT NULL", old_database) == '1'
        assert sql("SELECT count(*) FROM memory.user_profiles WHERE markdown<>''", old_database) == '1'
        mark('old_backup_missing_later_forget_is_blocked_and_isolated', stale_report)
        clean_report = privacy_check(reference, clean_snapshot, 'current_backup', 'privacy_matches_reference')
        mark('current_backup_matches_all_owner_privacy', clean_report)
        assert {db: fingerprints(db) for db in before_privacy} == before_privacy
        require_no_runtime_connections('financeclaw_java', restored, old_database)
        mark('privacy_snapshot_and_comparison_are_read_only')
        # Corrupt only the disposable stale clone to show epoch alone cannot bypass the check.
        sql(f'UPDATE memory.owners SET epoch={epoch + 1}', old_database)
        forged = privacy_snapshot(old_database, 'epoch-only-privacy')
        forged_report = privacy_check(reference, forged, 'epoch_only', 'blocked')
        assert forged_report['checks']['epochs_match']
        assert 'privacy_events_match' in forged_report['errors'] and 'no_unsafe_memory_rows' in forged_report['errors']
        mark('epoch_only_repair_does_not_hide_missing_audit_or_old_content', forged_report)
        sql("INSERT INTO memory.projections(fact_id,tenant,subject,epoch,revision,model_revision,dimensions,embedding) "
            "SELECT id,tenant,subject,epoch,revision,'m5e-synthetic',1024,array_fill(0.1::real,ARRAY[1024]) "
            "FROM memory.facts WHERE status='active'; "
            "INSERT INTO memory.turn_snapshots(turn_id,tenant,subject,epoch,profile_revision,context) "
            "SELECT id,tenant,subject,epoch,profile_revision,'synthetic-private-snapshot' FROM memory.sources "
            "WHERE status<>'forgotten' ON CONFLICT DO NOTHING; "
            "INSERT INTO agent_runtime.work_contexts(turn_id,conversation_id,tenant,subject,epoch,release_hash,grant_revision,attempt_id,state_version,payload,transcript) "
            "SELECT DISTINCT ON (t.id) t.id,t.conversation_id,t.tenant,t.subject,s.epoch,t.release_hash,1,a.id,1,'{}','[]' "
            "FROM business.turns t JOIN memory.sources s ON s.id=t.id JOIN business.commands c ON c.turn_id=t.id "
            "JOIN business.attempts a ON a.command_id=c.id WHERE s.status<>'forgotten' ORDER BY t.id,a.fence DESC "
            "ON CONFLICT(turn_id) DO UPDATE SET epoch=excluded.epoch; "
            f"INSERT INTO memory.audit(tenant,subject,epoch,action,target) VALUES "
            f"('local','developer',{epoch + 1},'forget','invalid-target'),"
            "('m5e-orphan','synthetic',2,'forget','all')", old_database)
        # 只污染从未接入运行时的旧备份克隆，分别验证已关联副本和完全未知的 SDK scope。
        sql("INSERT INTO business.child_invocations(root_turn_id,call_id,input_hash) "
            "SELECT t.id,'privacy-synthetic-child',repeat('0',64) FROM business.turns t "
            "WHERE t.tenant='local' AND t.subject='developer' ORDER BY t.id LIMIT 1; "
            "INSERT INTO business.capability_steps(turn_id,namespace,step,input_hash,result) "
            "SELECT root_turn_id,'child:'||call_id,'result',input_hash,'synthetic-private-step' "
            "FROM business.child_invocations WHERE call_id='privacy-synthetic-child'; "
            "INSERT INTO business.artifacts(id,turn_id,conversation_id,tenant,subject,release_hash,hash,payload,bytes,privacy_bound) "
            "SELECT 'privacy-synthetic-artifact',t.id,t.conversation_id,t.tenant,t.subject,t.release_hash,repeat('0',64),"
            "'synthetic-private-artifact',octet_length('synthetic-private-artifact'),true "
            "FROM business.turns t WHERE t.tenant='local' AND t.subject='developer' ORDER BY t.id LIMIT 1; "
            "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload) "
            "VALUES(repeat('a',64),repeat('b',64),repeat('c',64),'agent_state',1,'{}')", old_database)
        residual = privacy_snapshot(old_database, 'residual-privacy')
        residual_state = json.loads(residual.read_text())
        owner = next(o for o in residual_state['owners'] if o['tenant'] == 'local' and o['subject'] == 'developer')
        counters = ('unsafe_profiles', 'unsafe_sources', 'unsafe_facts', 'unsafe_projections',
                    'unsafe_snapshots', 'unsafe_work_contexts', 'unsafe_jobs', 'invalid_audit_targets',
                    'unsafe_sdk_states', 'unsafe_capability_steps', 'unsafe_child_invocations', 'unsafe_tool_artifacts')
        assert all(owner[k] > 0 for k in counters) and residual_state['orphan_owners'] > 0
        residual_report = privacy_check(reference, residual, 'residual_memory', 'blocked')
        assert 'no_orphan_owners' in residual_report['errors'] and 'no_unsafe_memory_rows' in residual_report['errors']
        mark('sql_detects_residual_memory_and_orphan_owners', {k: owner[k] for k in counters})
        assert residual_state['orphan_runtime_rows'] > 0
        assert 'no_orphan_runtime_rows' in residual_report['errors']
        mark('restore_v3_rejects_execution_residuals_and_unknown_live_states')
        # Delete only this synthetic, never-connected restore. No restore script repairs privacy.
        sql(f'DROP DATABASE {old_database}')
        mark('rejected_synthetic_restore_removed_before_runtime_start')
    restore_override = {'services': {s: {'environment': {
        'FINANCECLAW_DATABASE_URL' if s != 'memory-worker' else 'FINANCECLAW_MEMORY_DATABASE_URL':
        f'jdbc:postgresql://postgres:5432/{restored}'}} for s in ('api', 'memory-worker')}}
    (TEMP / 'restore.json').write_text(json.dumps(restore_override))
    BASE.extend(['-f', str(TEMP / 'restore.json')])
    compose('up', '-d', '--no-deps', '--wait', 'api', 'memory-worker')
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    assert api(wp)['status'] == 'completed'
    assert api('/v1/memory/privacy')['epoch'] == epoch + 1
    assert profile()[1] == ''
    if ARGS.admission:
        assert api('/v1/operations/admission') == {'paused': True, 'revision': pause['revision']}
        assert api('/v1/operations/drain')['drained']
        if ARGS.preflight:
            preflight('restored_pause', 'ready_for_review')
        rejected_submission(restored)
        admission(False, 'resume-after-restore')
        assert not api('/v1/operations/drain')['drained']
        assert sql('SELECT count(*) FROM business.admission_changes', restored) == '6'
        mark('restored_pause_and_audit_require_explicit_resume')
        if ARGS.preflight:
            preflight('reopened_admission', 'blocked')
    terminal(turn('new Java task after restored backup')[2])
    assert agent_admin('get', agent_id)['contentHash'] == updated['contentHash']
    assert agent_admin('history', agent_id)['nextAfterRevision'] == 2
    restored_agent_conversation, restored_agent_receipt, restored_agent_path = turn('P2a restored published request', agent_id)
    terminal(restored_agent_path)
    assert sql(f"SELECT release_hash FROM business.turns WHERE id='{restored_agent_receipt['turn_id']}'", restored) == agent_release_two
    assert sql(f"SELECT agent_id||':'||release_hash FROM business.conversations WHERE id='{old_agent_conversation}'", restored) == agent_id + ':' + agent_release_one
    assert sql(f"SELECT release_hash FROM business.turns WHERE id='{old_agent_receipt['turn_id']}'", restored) == agent_release_one
    mark('restored_agent_definitions_history_and_new_execution_keep_release_identity', {'application_tables': len(original)})
    assert agent_admin('history', composed_agent)['nextAfterRevision'] == 2
    _, restored_composed_receipt, restored_composed_path = turn('P2b restored composed agent', composed_agent)
    terminal(restored_composed_path)
    assert sql(f"SELECT release_hash FROM business.turns WHERE id='{restored_composed_receipt['turn_id']}'", restored) == function_release
    assert sql(f"SELECT release_hash FROM business.conversations WHERE id='{pure_conversation}'", restored) == empty_release
    mark('restored_composed_agent_executes_frozen_plan_with_same_runtime_artifact')
    # 备份在遗忘后生成：旧执行回执应已清除，普通业务报告仍按原策略保留。
    assert sql("SELECT count(*) FROM business.capability_steps WHERE step='publish_report'", restored) == '0'
    assert sql("SELECT count(*) FROM business.artifacts WHERE required_scope='workflow:run'", restored) == '1'
    mark('restored_runtime_keeps_results_privacy_and_accepts_new_turns')
    RESULT['jar_sha256'] = hashlib.sha256((ROOT / 'target/financeclaw-java-0.1.0-SNAPSHOT.jar').read_bytes()).hexdigest()
    RESULT['container_image'] = compose('images', '-q', 'api').decode().strip()
    assert all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h
               for p, h in RESULT['source_sha256'].items()), 'Deployment inputs changed during validation'
    RESULT['success'] = True
    RESULT['finished_at'] = datetime.now(timezone.utc).isoformat()
    (OUT / 'compose-results.json').write_text(json.dumps(RESULT, ensure_ascii=False, indent=2)+'\n')
    print('Compose validation passed;', OUT / 'compose-results.json', flush=True)
finally:
    # Only this script's random, freshly-created project is ever removed, including on failure.
    try:
        LOG.write(compose('logs', '--no-color', '--tail', '50').decode(errors='replace'))
        compose('down', '--volumes', '--remove-orphans')
    finally:
        LOG.close()
