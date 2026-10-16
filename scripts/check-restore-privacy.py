#!/usr/bin/env python3
"""Compare private SQL privacy snapshots before connecting a restored database to any runtime."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

spec = importlib.util.spec_from_file_location('drain_files', Path(__file__).with_name('inspect-drain.py'))
files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(files)
MAX_BYTES = 16 * 1024 * 1024
COUNTERS = ('unsafe_sources', 'unsafe_facts', 'unsafe_projections', 'unsafe_recalls',
            'unsafe_jobs', 'invalid_audit_targets')


def require(value):
    files.require(value, 'invalid_snapshot')


def timestamp(value):
    require(isinstance(value, str) and len(value) <= 40)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.utcoffset() is not None)
    return parsed


def snapshot(raw):
    require(type(raw) is dict and set(raw) == {'schema_version', 'scope', 'consistency',
                                              'captured_at', 'orphan_owners', 'owners'})
    require(type(raw['schema_version']) is int and raw['schema_version'] == 1
            and raw['scope'] == 'all_memory_owners'
            and raw['consistency'] == 'repeatable_read_read_only')
    timestamp(raw['captured_at'])
    files.number(raw['orphan_owners'])
    require(type(raw['owners']) is list and len(raw['owners']) <= 100000)
    owners = {}
    audit_ids = set()
    for row in raw['owners']:
        require(type(row) is dict and set(row) == {'tenant', 'subject', 'epoch', 'purge_pending',
                                                  'events', *COUNTERS})
        for key in ('tenant', 'subject'):
            require(isinstance(row[key], str) and 0 < len(row[key]) <= 1024)
        key = (row['tenant'], row['subject'])
        require(key not in owners and type(row['purge_pending']) is bool)
        files.number(row['epoch'], 1)
        for counter in COUNTERS:
            files.number(row[counter])
        require(type(row['events']) is list)
        previous = 0
        for event in row['events']:
            require(type(event) is dict and set(event) == {'id', 'epoch', 'action', 'at'})
            files.number(event['id'], 1)
            files.number(event['epoch'], 2)
            require(event['id'] > previous and event['id'] not in audit_ids)
            require(event['action'] in ('forget', 'purge_complete'))
            timestamp(event['at'])
            previous = event['id']
            audit_ids.add(previous)
        owners[key] = row
    return owners


def compare(reference, restored):
    report = {'schema_version': 1, 'scope': 'all_memory_owners',
              'assessment': 'incomplete', 'errors': [], 'checks': {},
              'consistency': 'two_supplied_snapshots',
              'checked_at': datetime.now(timezone.utc).isoformat()}
    try:
        expected, actual = snapshot(reference), snapshot(restored)
        checks = report['checks']
        checks['owner_sets_match'] = expected.keys() == actual.keys()
        checks['no_orphan_owners'] = reference['orphan_owners'] == restored['orphan_owners'] == 0
        checks['epochs_match'] = checks['owner_sets_match'] and all(
            expected[k]['epoch'] == actual[k]['epoch'] for k in expected)
        checks['privacy_events_match'] = checks['owner_sets_match'] and all(
            expected[k]['events'] == actual[k]['events'] for k in expected)
        checks['no_pending_purge'] = all(not r['purge_pending'] for r in [*expected.values(), *actual.values()])
        checks['no_unsafe_memory_rows'] = all(not r[c] for r in [*expected.values(), *actual.values()] for c in COUNTERS)
        # Every increment must retain a forget fact. purge_complete may coalesce earlier requests,
        # but the latest epoch needs its completion after its own forget event.
        def complete(row):
            forgets = [e for e in row['events'] if e['action'] == 'forget']
            if len(forgets) != row['epoch'] - 1 or any(e['epoch'] != n for n, e in enumerate(forgets, 2)):
                return False
            if any(e['epoch'] > row['epoch'] for e in row['events']):
                return False
            by_epoch = {e['epoch']: e['id'] for e in forgets}
            purges = [e for e in row['events'] if e['action'] == 'purge_complete']
            if any(e['epoch'] not in by_epoch or e['id'] <= by_epoch[e['epoch']] for e in purges):
                return False
            return row['epoch'] == 1 or any(e['epoch'] == row['epoch'] for e in purges)
        checks['audit_history_complete'] = all(complete(r) for r in [*expected.values(), *actual.values()])
        report['owner_counts'] = {'reference': len(expected), 'restored': len(actual)}
        report['errors'] = [k for k, ok in checks.items() if not ok]
        report['assessment'] = 'privacy_matches_reference' if not report['errors'] else 'blocked'
    except (files.Invalid, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        report['errors'] = ['invalid_snapshot']
    return report


def read(path):
    with path.open('rb') as source:
        raw = source.read(MAX_BYTES + 1)
    files.require(len(raw) <= MAX_BYTES, 'snapshot_too_large')
    return json.loads(raw, object_pairs_hook=files.unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(files.Invalid('invalid_json'))), hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True, type=Path, help='Latest authoritative snapshot after writers stop')
    parser.add_argument('--restored', required=True, type=Path, help='Snapshot from the isolated restored database')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        reference, ref_hash = read(args.reference)
        restored, restored_hash = read(args.restored)
        report = compare(reference, restored)
        report['input_sha256'] = {'reference': ref_hash, 'restored': restored_hash}
    except (files.Invalid, ValueError, UnicodeError, RecursionError):
        report = {'schema_version': 1, 'assessment': 'incomplete', 'errors': ['invalid_snapshot']}
    except OSError:
        print('Privacy check failed: local_file_error', file=sys.stderr)
        return 1
    try:
        report['source_sha256'] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in ('check-restore-privacy.py', 'inspect-drain.py')}
        path = args.output or Path('target/m5e') / ('privacy-' + uuid.uuid4().hex + '.json')
        files.save(report, path)
    except (OSError, UnicodeError):
        print('Privacy check failed: local_file_error', file=sys.stderr)
        return 1
    print(report['assessment'] + '; report: ' + str(path.resolve()))
    return {'privacy_matches_reference': 0, 'blocked': 2, 'incomplete': 3}[report['assessment']]


if __name__ == '__main__':
    raise SystemExit(main())
