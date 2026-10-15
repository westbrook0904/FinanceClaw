#!/usr/bin/env python3
"""Isolated delivery/failure/backup experiment, optionally with the M6d Ziwei development policy."""
import concurrent.futures
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--ziwei', action='store_true', help='Also boot the shared Ziwei development policy and secret configuration')
ARGS = parser.parse_args()
OUT = ROOT / ('target/m6/deployment' if ARGS.ziwei else 'target/m5')
OUT.mkdir(parents=True, exist_ok=True)
THRESHOLDS = json.loads((ROOT / 'ops/m5-thresholds.json').read_text())
PROJECT = ('financeclaw-m6d-' if ARGS.ziwei else 'financeclaw-m5-') + uuid.uuid4().hex[:10]
TEMP = ROOT / '.cache' / PROJECT
TEMP.mkdir(parents=True)
ENV = os.environ.copy()
# This experiment cannot inherit a configured live provider, tenant or Feishu channel.
ENV.update({k: '' for k in ENV if k.startswith('FINANCECLAW_')})
ENV.update(FINANCECLAW_HTTP_PORT='0', FINANCECLAW_MODEL='offline',
           FINANCECLAW_ZIWEI_ENABLED='false', FINANCECLAW_ZIWEI_DEVELOPMENT='false',
           FINANCECLAW_HOTEL_ENABLED='false', FINANCECLAW_TAIBU_ENABLED='false',
           FINANCECLAW_FEISHU_ENABLED='false', FINANCECLAW_MEMORY_ENABLED='true',
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
override = {'services': {s: {
    'environment': {'FINANCECLAW_MARKET_FILE': '/app/market.json'},
    'volumes': [f'{TEMP / "market.json"}:/app/market.json:ro']}
    for s in ('api', 'worker', 'integrations')}}
(TEMP / 'override.json').write_text(json.dumps(override))
BASE = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(TEMP / 'empty.env'),
        '-p', PROJECT, '-f', str(ROOT / 'compose.yml'), '-f', str(TEMP / 'override.json')]
LOG = (OUT / 'compose-validation.log').open('w')
RESULT = {'scope': 'isolated real containers; offline model and synthetic market only',
          'ziwei_development_policy': ARGS.ziwei,
          'project': PROJECT, 'thresholds': THRESHOLDS, 'checks': {},
          'started_at': datetime.now(timezone.utc).isoformat()}
INPUTS = ('Dockerfile', '.dockerignore', 'compose.yml', 'ops/postgres-init.sql',
          'ops/container-entrypoint.sh', 'ops/m5-thresholds.json', 'scripts/validate-compose.py')
RESULT['source_sha256'] = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in INPUTS}
(OUT / 'compose-results.json').unlink(missing_ok=True)
URL = ''
TOKEN = (ROOT / '.secrets/api-token').read_text()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(args, *, data=None, timeout=240):
    result = subprocess.run(args, cwd=ROOT, env=ENV, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    LOG.write(result.stderr.decode(errors='replace'))
    LOG.flush()
    if result.returncode:
        raise RuntimeError(f'Command failed (exit {result.returncode}); see {OUT}/compose-validation.log')
    return result.stdout


def compose(*args, **kw):
    return command(BASE + list(args), **kw)


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


def api(path, body=None, key=None):
    status, result = request(path, body, key)
    assert status in (200, 201, 202), (path, status)
    return result


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


def turn(message='M5 synthetic request'):
    c = api('/v1/conversations', {})['conversation_id']
    receipt = api(f'/v1/conversations/{c}/turns', {'message': message}, 'admit-once')
    path = f'/v1/conversations/{c}/turns/{receipt["turn_id"]}'
    return c, receipt, path


def terminal(path, status='completed'):
    return wait(lambda: (value if (value := api(path))['status'] == status else None),
                THRESHOLDS['recovery_seconds'])


def fingerprints(database):
    tables = sql("SELECT schemaname||'.'||tablename FROM pg_tables WHERE schemaname IN "
                 "('business','agent_runtime','memory','channel') ORDER BY 1", database).splitlines()
    return {table: sql("SELECT count(*)||':'||md5(COALESCE(string_agg(md5(to_jsonb(t)::text),'' "
                       f"ORDER BY md5(to_jsonb(t)::text)),'')) FROM {table} t", database)
            for table in tables}


try:
    print('Starting isolated deployment validation:', PROJECT, flush=True)
    compose('config', '--quiet')
    if ARGS.ziwei:
        config = json.loads(compose('config', '--format', 'json'))
        for role in ('api', 'worker', 'integrations'):
            service = config['services'][role]
            assert service['environment']['FINANCECLAW_ZIWEI_ENABLED'] == 'true'
            assert service['environment']['FINANCECLAW_ZIWEI_DEVELOPMENT'] == 'true'
            assert service['environment']['FINANCECLAW_ZIWEI_KEY_REF'] == 'ZIWEI_HMAC_KEY'
            assert any(s['source'] == 'ziwei_key' and s['target'] == 'ZIWEI_HMAC_KEY'
                       for s in service['secrets'])
        for role in ('migrate', 'memory-worker'):
            assert not any(s['source'] == 'ziwei_key' for s in config['services'][role]['secrets'])
        mark('shared_ziwei_policy_and_role_scoped_secret_mounts')
    compose('build', 'api')
    started = time.monotonic()
    compose('up', '-d', '--wait', '--wait-timeout', str(THRESHOLDS['startup_seconds']))
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)
    assert request('/v1/operations', auth=False)[0] == 401
    assert sql('SELECT count(*) FROM public.flyway_schema_history WHERE success') == '5'
    mark('empty_database_and_four_healthy_roles', {'startup_seconds': time.monotonic()-started})
    assert sql("SELECT has_schema_privilege('financeclaw_memory','business','USAGE')") == 'f'
    assert sql("SELECT has_schema_privilege('financeclaw_app','business','CREATE')") == 'f'
    for role in ('worker', 'integrations', 'memory-worker'):
        code = compose('exec', '-T', role, 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                       'http://localhost:8080/v1/conversations').decode()
        assert code == '404', (role, code)
    mark('background_http_and_database_privilege_boundaries')

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
    wait(lambda: api('/v1/operations')['outbox']['pending'] == THRESHOLDS['remaining_outbox'])
    mark('offline_load_and_idempotency', {'requests': len(durations), 'success_ratio': 1,
                                        'p95_seconds': p95, 'provider_calls': 0, 'provider_cost': 0})

    compose('stop', 'worker')
    c, receipt, path = turn('durable admission across API restart')
    assert api(path)['status'] == 'queued'
    compose('restart', 'api')
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)
    replay = api(f'/v1/conversations/{c}/turns', {'message': 'durable admission across API restart'}, 'admit-once')
    assert replay['turn_id'] == receipt['turn_id']
    compose('up', '-d', '--no-deps', '--wait', 'worker')
    terminal(path)
    mark('graceful_worker_stop_and_api_restart_keep_admission')

    workflow = '/workflow portfolio_review ' + json.dumps({'portfolio_name': 'M5 synthetic',
        'positions': [{'symbol': 'AAPL', 'quantity': '1', 'cost_basis': '90'}]})
    wc, wr, wp = turn(workflow)
    terminal(wp, 'waiting')
    question = api(wp + '/interactions')[0]
    assert question['kind'] == 'approval'
    compose('kill', '-s', 'SIGKILL', 'worker')
    compose('up', '-d', '--no-deps', '--wait', 'worker')
    answer = {'kind': 'approval', 'revision': question['revision'], 'decision': 'approve',
              'action_hash': question['action_hash'], 'answer': None}
    api(wp + '/interactions/' + question['id'] + '/answer', answer, 'approval-once')
    terminal(wp)
    api(wp + '/interactions/' + question['id'] + '/answer', answer, 'approval-once')
    assert sql("SELECT count(*) FROM business.capability_steps WHERE step='publish_report'") == '1'
    assert sql("SELECT count(*) FROM business.artifacts WHERE required_scope='workflow:run'") == '1'
    mark('sigkill_and_persisted_approval_execute_once')

    compose('stop', 'postgres')
    assert request('/actuator/health/readiness', auth=False)[0] == 503
    assert request(wp)[0] == 503
    recovery = time.monotonic()
    compose('start', 'postgres')
    wait(lambda: request('/actuator/health/readiness', auth=False)[0] == 200)
    assert api(wp)['status'] == 'completed'
    after = turn('after database recovery')
    terminal(after[2])
    elapsed = time.monotonic()-recovery
    assert elapsed < THRESHOLDS['recovery_seconds']
    mark('database_outage_fails_readiness_then_recovers', {'recovery_seconds': elapsed})

    mc, mr, mp = turn('/remember response_style=简洁')
    terminal(mp)
    wait(lambda: any(f['status'] == 'active' for f in api('/v1/memory')))
    epoch = api('/v1/memory/privacy')['epoch']
    assert api('/v1/memory/forget', {'epoch': epoch}, 'forget-once')['epoch'] == epoch + 1
    wait(lambda: not api('/v1/memory/privacy')['purge_pending'])
    assert api('/v1/memory') == []
    mark('restricted_memory_worker_and_forgetting')
    wait(lambda: api('/v1/operations')['outbox']['pending'] == 0)
    RESULT['operations'] = api('/v1/operations')

    compose('stop', 'api', 'worker', 'integrations', 'memory-worker')
    original = fingerprints('financeclaw_java')
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
    mark('consistent_backup_restore_all_tables_and_privacy_epoch',
         {'tables': len(original), 'sha256': hashlib.sha256(backup).hexdigest(), 'rows_and_hashes': original})
    restore_override = {'services': {s: {'environment': {
        'FINANCECLAW_DATABASE_URL' if s != 'memory-worker' else 'FINANCECLAW_MEMORY_DATABASE_URL':
        f'jdbc:postgresql://postgres:5432/{restored}'}} for s in ('api', 'worker', 'integrations', 'memory-worker')}}
    (TEMP / 'restore.json').write_text(json.dumps(restore_override))
    BASE.extend(['-f', str(TEMP / 'restore.json')])
    compose('up', '-d', '--no-deps', '--wait', 'api', 'worker', 'integrations', 'memory-worker')
    URL = 'http://' + compose('port', 'api', '8080').decode().strip()
    assert api(wp)['status'] == 'completed'
    assert api('/v1/memory/privacy')['epoch'] == epoch + 1
    terminal(turn('new Java task after restored backup')[2])
    assert sql("SELECT count(*) FROM business.capability_steps WHERE step='publish_report'", restored) == '1'
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
