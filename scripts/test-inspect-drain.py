#!/usr/bin/env python3
"""Offline and loopback contract tests for read-only drain preflight."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

spec = importlib.util.spec_from_file_location('preflight', Path(__file__).with_name('inspect-drain.py'))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class Fixture:
    def __init__(self):
        self.state = {'scope': 'current_owner', 'admission': {'paused': True, 'revision': 2},
                      'counts': dict.fromkeys(preflight.COUNTS, 0), 'drained': True}
        self.state.update(tenant='private-owner', subject='private-owner')
        self.items = {kind: [] for kind in preflight.KINDS}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == '/v1/operations/drain':
            return copy.deepcopy(self.state)
        query = parse_qs(urlsplit(path).query, keep_blank_values=True)
        kind, after, limit = query['kind'][0], query['after'][0], int(query['limit'][0])
        key = int if kind == 'memory_jobs' else str
        rows = [r for r in self.items[kind] if key(r['id']) > key(after or ('0' if kind == 'memory_jobs' else ''))]
        page = rows[:limit]
        return copy.deepcopy({'scope': 'current_owner', 'items': page,
                              'next_after': page[-1]['id'] if len(rows) > limit else None})

    def block(self):
        self.state['drained'] = False
        self.state['counts'].update(waiting=1, pending_interactions=1, unverified_notifications=1,
                                    pending_memory_jobs=2, pending_forget=1)
        self.items['turns'] = [{'id': 'turn-1', 'conversation_id': 'conversation-1', 'status': 'waiting',
            'revision': 4, 'reason': None, 'command_status': 'done', 'pending_interactions': 1,
            'uncertain_effects': 0, 'pending_outbox': 0, 'message': 'secret-message'}]
        self.items['notifications'] = [{'id': 'notice-1', 'conversation_id': 'conversation-1',
            'turn_id': 'turn-1', 'status': 'submitted_unverified', 'revision': 4, 'payload': 'secret-notice'}]
        self.items['memory_jobs'] = [{'id': str(n), 'kind': 'extract', 'status': 'pending', 'revision': 1,
            'epoch': 1, 'attempts': 2, 'error_code': 'extraction_failed', 'lease_token': 'secret-token'} for n in (9, 10)]


class Server:
    def __init__(self, response):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.command, self.path, self.headers.get('Authorization')))
                status, headers, body = response(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class PreflightTest(unittest.TestCase):
    def test_empty_paused_owner_is_ready_for_review_and_identity_is_not_exported(self):
        report = preflight.inspect(Fixture().get)
        self.assertEqual(report['assessment'], 'ready_for_review')
        self.assertTrue(report['observations_match'] and report['inventory_matches_counts'])
        self.assertEqual(report['consistency'], 'separate_live_reads')
        self.assertNotIn('private-owner', json.dumps(report))

    def test_open_admission_and_real_backlogs_are_blocked_and_content_is_minimized(self):
        fixture = Fixture()
        fixture.state['admission']['paused'] = False
        fixture.state['drained'] = False
        self.assertEqual(preflight.inspect(fixture.get)['assessment'], 'blocked')
        fixture.block()
        report = preflight.inspect(fixture.get, page_size=1)
        self.assertEqual(report['assessment'], 'blocked')
        self.assertEqual([r['id'] for r in report['items']['memory_jobs']], ['9', '10'])
        self.assertNotIn('secret-', json.dumps(report))

    def test_changed_admission_forget_or_counts_are_incomplete(self):
        for change in ('admission', 'forget', 'counts'):
            with self.subTest(change=change):
                fixture = Fixture()
                def get(path):
                    if 'items?' in path:
                        if change == 'admission': fixture.state['admission']['revision'] = 3
                        elif change == 'forget':
                            fixture.state['counts']['pending_forget'] = 1
                            fixture.state['drained'] = False
                        else:
                            fixture.state['counts']['blocked'] = 1
                            fixture.state['drained'] = False
                    return fixture.get(path)
                report = preflight.inspect(get)
                self.assertEqual(report['assessment'], 'incomplete')
                self.assertIn('observations_changed', report['errors'])

    def test_matching_counts_at_both_ends_do_not_hide_missing_inventory(self):
        fixture = Fixture()
        fixture.state['counts']['uncertain_effects'] = 1
        fixture.state['drained'] = False
        report = preflight.inspect(fixture.get)
        self.assertEqual(report['assessment'], 'incomplete')
        self.assertIn('inventory_count_mismatch', report['errors'])

    def test_truncation_does_not_claim_complete_inventory(self):
        fixture = Fixture()
        fixture.block()
        report = preflight.inspect(fixture.get, page_size=1, max_pages=1)
        self.assertEqual(report['assessment'], 'incomplete')
        self.assertIn('page_limit_memory_jobs', report['errors'])

    def test_repeated_or_forged_cursors_and_wrong_owner_scope_fail_closed(self):
        for mutation in ('repeat', 'cursor', 'scope'):
            with self.subTest(mutation=mutation):
                fixture = Fixture()
                fixture.block()
                def get(path):
                    raw = fixture.get(path)
                    if 'kind=memory_jobs' in path:
                        if mutation == 'repeat':
                            raw['items'] = [fixture.items['memory_jobs'][0]]
                            raw['next_after'] = '9'
                        elif mutation == 'cursor': raw['next_after'] = 'fabricated'
                        else: raw['scope'] = 'all_owners'
                    return raw
                self.assertEqual(preflight.inspect(get, page_size=1)['assessment'], 'incomplete')

    def test_invalid_schemas_never_become_ready(self):
        for mutate in (lambda r: r['counts'].update(queued=True), lambda r: r.update(drained='true'),
                       lambda r: r['counts'].update(unknown_counter=0), lambda r: r['admission'].update(revision=-1)):
            fixture = Fixture()
            mutate(fixture.state)
            self.assertEqual(preflight.inspect(fixture.get)['assessment'], 'incomplete')
        fixture = Fixture()
        fixture.block()
        fixture.items['turns'][0]['reason'] = 'secret raw exception text'
        report = preflight.inspect(fixture.get)
        self.assertEqual(report['assessment'], 'incomplete')
        self.assertNotIn('secret raw', json.dumps(report))

    def test_actual_http_is_get_only_and_does_not_follow_redirects(self):
        fixture = Fixture()
        with Server(lambda p: (200, {}, json.dumps(fixture.get(p)).encode())) as server:
            report = preflight.inspect(preflight.client(server.url, 'test-only-token', 2))
            self.assertEqual(report['assessment'], 'ready_for_review')
            self.assertTrue(all(method == 'GET' and auth == 'Bearer test-only-token'
                                for method, path, auth in server.requests))
            self.assertTrue(all(path.startswith('/v1/operations/drain') for _, path, _ in server.requests))
        with Server(lambda p: (302, {'Location': '/credential-leak'}, b'secret-body')) as server:
            report = preflight.inspect(preflight.client(server.url, 'test-only-token', 2))
            self.assertEqual(report['errors'], ['http_302'])
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn('secret', json.dumps(report))

    def test_http_errors_duplicate_keys_and_oversized_responses_are_sanitized(self):
        for status, body, expected in ((401, b'secret-body', 'http_401'),
                (200, b'{"a":1,"a":2}', 'duplicate_json_key'),
                (200, b'"' + b'x' * preflight.MAX_BYTES + b'"', 'response_too_large'),
                (200, b'secret invalid JSON', 'invalid_json'),
                (200, b'[' * 2000 + b']' * 2000, ('invalid_json', 'invalid_response'))):
            with self.subTest(expected=expected), Server(lambda p: (status, {}, body)) as server:
                report = preflight.inspect(preflight.client(server.url, 'test-only-token', 2))
                self.assertEqual(report['assessment'], 'incomplete')
                self.assertEqual(len(report['errors']), 1)
                self.assertIn(report['errors'][0], (expected,) if isinstance(expected, str) else expected)
                self.assertNotIn('secret', json.dumps(report))

    def test_invalid_destination_and_limits_do_not_send_requests(self):
        for url in ('http://example.com', 'https://u:p@example.com', 'https://example.com/path',
                    'https://example.com?token=secret', 'ftp://localhost', 'http://[::1]:bad',
                    'http://localhost\n', 'https://@example.com'):
            with self.subTest(url=url), self.assertRaises(preflight.Invalid):
                preflight.client(url, 'test-only-token', 2)
        for token in ('', 'x\r\ny', 'null\0byte', '令牌'):
            with self.assertRaises(preflight.Invalid):
                preflight.client('http://127.0.0.1:1', token, 2)
        fixture = Fixture()
        self.assertEqual(preflight.inspect(fixture.get, page_size=101)['errors'], ['invalid_limits'])
        self.assertEqual(fixture.calls, [])

    def test_private_atomic_reports_never_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'evidence.json'
            report = preflight.inspect(Fixture().get)
            preflight.save(report, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), report)
            with self.assertRaises(FileExistsError): preflight.save({'changed': True}, path)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_cli_exit_codes_and_artifacts_match_observed_state(self):
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as folder, Server(lambda p: (200, {}, json.dumps(fixture.get(p)).encode())) as server:
            token = Path(folder) / 'token'
            token.write_text('test-only-token')
            for expected in (0, 2):
                path = Path(folder) / f'report-{expected}.json'
                if expected == 2: fixture.block()
                run = subprocess.run([sys.executable, str(Path(__file__).with_name('inspect-drain.py')),
                    '--base-url', server.url, '--token-file', str(token), '--output', str(path)],
                    capture_output=True, text=True, timeout=10, env={**os.environ, 'FINANCECLAW_API_TOKEN': 'ignored-value'})
                self.assertEqual(run.returncode, expected, run.stderr)
                self.assertTrue(path.exists())
                self.assertNotIn('test-only-token', run.stdout + run.stderr + path.read_text())


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PreflightTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    root = Path(__file__).resolve().parents[1]
    out = root / 'target/m5d/preflight-tests.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped),
        'source_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                          for name in ('inspect-drain.py', 'test-inspect-drain.py')},
        'scope': 'offline fixtures, loopback HTTP and local CLI; no real deployment or provider'}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
