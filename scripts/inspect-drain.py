#!/usr/bin/env python3
"""Read-only, owner-scoped drain observations. Never changes admission or authorizes cutover."""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

COUNTS = ('queued', 'running', 'waiting', 'cancelling', 'blocked', 'active_commands',
          'pending_interactions', 'pending_notifications',
          'unverified_notifications', 'unprojected_notifications', 'pending_memory_jobs', 'pending_forget')
KINDS = ('turns', 'notifications', 'notification_bindings', 'memory_jobs')
ACTIVE = ('queued', 'resuming', 'running', 'waiting', 'cancelling', 'blocked')
TURN_STATES = (*ACTIVE, 'completed', 'failed', 'cancelled')
MAX_BYTES = 262144


class Invalid(Exception):
    pass


def require(value, code='invalid_response'):
    if not value:
        raise Invalid(code)


def number(value, minimum=0):
    require(type(value) is int and minimum <= value <= 9223372036854775807)
    return value


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value))
    return value


def code(value):
    require(value is None or isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,128}', value))
    return value


def drain(raw):
    require(type(raw) is dict and raw.get('scope') == 'current_owner')
    state, counts = raw.get('admission'), raw.get('counts')
    require(type(state) is dict and type(state.get('paused')) is bool)
    require(type(counts) is dict and set(counts) == set(COUNTS))
    clean = {'scope': 'current_owner', 'admission': {'paused': state['paused'],
             'revision': number(state.get('revision'))}, 'counts': {k: number(counts[k]) for k in COUNTS}}
    require(type(raw.get('drained')) is bool)
    require(raw['drained'] == (state['paused'] and not any(clean['counts'].values())))
    require(clean['counts']['pending_forget'] in (0, 1))
    clean['drained'] = raw['drained']
    return clean


def item(raw, kind):
    require(type(raw) is dict)
    clean = {'id': identifier(raw.get('id')), 'status': raw.get('status'),
             'revision': number(raw.get('revision'), 1)}
    if kind == 'memory_jobs':
        require(re.fullmatch(r'[1-9][0-9]*', clean['id']) and int(clean['id']) <= 9223372036854775807)
        require(raw.get('kind') in ('extract', 'index'))
        require(isinstance(clean['status'], str) and re.fullmatch(r'[a-z0-9_]{1,64}', clean['status']))
        require(clean['status'] not in ('done', 'obsolete', 'forgotten'))
        clean.update(kind=raw['kind'], epoch=number(raw.get('epoch'), 1),
                     attempts=number(raw.get('attempts')), error_code=code(raw.get('error_code')))
    else:
        clean['conversation_id'] = identifier(raw.get('conversation_id'))
        if kind == 'turns':
            require(clean['status'] in TURN_STATES)
            require(raw.get('command_status') in (None, 'queued', 'running', 'done'))
            clean.update(command_status=raw.get('command_status'), reason=code(raw.get('reason')))
            for key in ('pending_interactions',):
                clean[key] = number(raw.get(key))
            require(clean['status'] in ACTIVE or clean['command_status'] in ('queued', 'running')
                    or any(clean[k] for k in ('pending_interactions',)))
        else:
            clean['turn_id'] = identifier(raw.get('turn_id'))
            require(clean['status'] in (('pending', 'sending', 'unknown', 'submitted_unverified')
                    if kind == 'notifications' else ('waiting', 'blocked', 'completed', 'failed', 'cancelled')))
    return clean


def inspect(get, page_size=50, max_pages=20):
    report = {'schema_version': 1, 'scope': 'current_owner', 'consistency': 'separate_live_reads',
              'assessment': 'incomplete', 'items': {}, 'errors': [],
              'started_at': datetime.now(timezone.utc).isoformat()}
    try:
        require(1 <= page_size <= 100 and 1 <= max_pages <= 100, 'invalid_limits')
        report['before'] = drain(get('/v1/operations/drain'))
        for kind in KINDS:
            rows, after = [], ''
            report['items'][kind] = rows
            for _ in range(max_pages):
                query = urllib.parse.urlencode({'kind': kind, 'limit': page_size, 'after': after})
                raw = get('/v1/operations/drain/items?' + query)
                require(type(raw) is dict and raw.get('scope') == 'current_owner')
                require(type(raw.get('items')) is list and len(raw['items']) <= page_size)
                page = [item(row, kind) for row in raw['items']]
                prior = int(after or '0') if kind == 'memory_jobs' else after
                for row in page:
                    current = int(row['id']) if kind == 'memory_jobs' else row['id']
                    require(current > prior, 'nonadvancing_page')
                    prior = current
                require('next_after' in raw)
                next_after = raw['next_after']
                require(next_after is None or page and next_after == page[-1]['id'], 'invalid_cursor')
                rows.extend(page)
                if next_after is None:
                    break
                after = next_after
            else:
                report['errors'].append('page_limit_' + kind)
        report['after'] = drain(get('/v1/operations/drain'))
        report['observations_match'] = report['before'] == report['after']
        if not report['observations_match']:
            report['errors'].append('observations_changed')
        observed = dict.fromkeys(COUNTS, 0)
        for row in report['items']['turns']:
            state = 'queued' if row['status'] == 'resuming' else row['status']
            if state in ACTIVE:
                observed[state] += 1
            observed['active_commands'] += row['command_status'] in ('queued', 'running')
            for key in ('pending_interactions',):
                observed[key] += row[key]
        for row in report['items']['notifications']:
            observed['pending_notifications' if row['status'] in ('pending', 'sending')
                     else 'unverified_notifications'] += 1
        observed['unprojected_notifications'] = len(report['items']['notification_bindings'])
        observed['pending_memory_jobs'] = len(report['items']['memory_jobs'])
        # A single owner-level blocker, already present in the read-only count snapshot.
        observed['pending_forget'] = report['after']['counts']['pending_forget']
        report['inventory_matches_counts'] = observed == report['after']['counts']
        if not report['inventory_matches_counts']:
            report['errors'].append('inventory_count_mismatch')
        if not report['errors']:
            report['assessment'] = 'ready_for_review' if report['after']['drained'] else 'blocked'
    except Invalid as error:
        report['errors'].append(str(error))
    except (KeyError, TypeError, ValueError):
        report['errors'].append('invalid_response')
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return report


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def base_url(value):
    require(isinstance(value, str) and not any(c.isspace() for c in value), 'invalid_base_url')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise Invalid('invalid_base_url') from None
    require(parsed.hostname and parsed.scheme in ('http', 'https') and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment
            and parsed.path in ('', '/') and (port is None or port > 0), 'invalid_base_url')
    require(parsed.scheme == 'https' or parsed.hostname in ('127.0.0.1', 'localhost', '::1'),
            'https_required_for_remote')
    return value.rstrip('/')


def client(base, token, timeout):
    base = base_url(base)
    require(isinstance(token, str) and 1 <= len(token) <= 8192
            and all(33 <= ord(c) <= 126 for c in token), 'invalid_token')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(path):
        require(path.startswith('/v1/') and not path.startswith('//'), 'invalid_path')
        req = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + token}, method='GET')
        try:
            with opener.open(req, timeout=timeout) as response:
                require(response.status == 200, 'unexpected_http_status')
                raw = response.read(MAX_BYTES + 1)
                require(len(raw) <= MAX_BYTES, 'response_too_large')
                return json.loads(raw, object_pairs_hook=unique_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(Invalid('invalid_json')))
        except urllib.error.HTTPError as error:
            raise Invalid('http_' + str(error.code)) from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
            raise Invalid('transport_failed') from None
        except (ValueError, UnicodeError, RecursionError):
            raise Invalid('invalid_json') from None
    return get


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def save(report, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete private file atomically; never replace an earlier report.
    fd, name = tempfile.mkstemp(prefix='.drain-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--token-file', type=Path, help='Otherwise use FINANCECLAW_API_TOKEN; never pass the token as an argument')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--page-size', type=int, default=50)
    parser.add_argument('--max-pages', type=int, default=20)
    parser.add_argument('--timeout', type=int, default=10)
    args = parser.parse_args()
    try:
        require(1 <= args.timeout <= 30 and 1 <= args.page_size <= 100 and 1 <= args.max_pages <= 100,
                'invalid_limits')
        token = args.token_file.read_text().strip() if args.token_file else os.environ.get('FINANCECLAW_API_TOKEN', '')
        report = inspect(client(args.base_url, token, args.timeout), args.page_size, args.max_pages)
        report['source_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        path = args.output or Path('target/m5d') / ('preflight-' + uuid.uuid4().hex + '.json')
        save(report, path)
    except (Invalid, OSError, UnicodeError) as error:
        print('Preflight could not start or save:', str(error) if isinstance(error, Invalid) else 'local_file_error')
        return 1
    print(report['assessment'] + '; report: ' + str(path.resolve()))
    return {'ready_for_review': 0, 'blocked': 2, 'incomplete': 3}[report['assessment']]


if __name__ == '__main__':
    raise SystemExit(main())
