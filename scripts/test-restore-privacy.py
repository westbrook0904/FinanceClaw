#!/usr/bin/env python3
"""Offline contracts for all-owner restore privacy comparisons; no database or credentials."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('privacy', Path(__file__).with_name('check-restore-privacy.py'))
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)
AT = '2026-10-15T01:00:00.000000Z'


def fixture(epoch=2):
    row = {'tenant': 'private-tenant', 'subject': 'private-subject', 'epoch': epoch,
           'purge_pending': False, 'events': [], **dict.fromkeys(privacy.COUNTERS, 0)}
    for n in range(2, epoch + 1):
        row['events'].extend([{'id': n * 2, 'epoch': n, 'action': 'forget', 'at': AT},
                              {'id': n * 2 + 1, 'epoch': n, 'action': 'purge_complete', 'at': AT}])
    return {'schema_version': 3, 'scope': 'all_memory_owners',
            'consistency': 'repeatable_read_read_only', 'captured_at': AT,
            'orphan_owners': 0, 'orphan_runtime_rows': 0, 'owners': [row]}


class RestorePrivacyTest(unittest.TestCase):
    def test_v2_cannot_omit_execution_residuals_and_unknown_states(self):
        legacy = fixture()
        legacy['schema_version'] = 2
        legacy.pop('orphan_runtime_rows')
        for owner in legacy['owners']:
            for key in ('unsafe_sdk_states', 'unsafe_capability_steps', 'unsafe_child_invocations', 'unsafe_tool_artifacts'):
                owner.pop(key)
        for left, right in ((legacy, legacy), (legacy, fixture()), (fixture(), legacy)):
            report = privacy.compare(left, right)
            self.assertEqual(report['assessment'], 'incomplete')
            self.assertEqual(report['errors'], ['invalid_snapshot'])

    def test_unknown_execution_state_blocks_even_with_clean_owner_counters(self):
        source, dirty = fixture(), fixture()
        dirty['orphan_runtime_rows'] = 1
        for left, right in ((source, dirty), (dirty, source)):
            self.assertIn('no_orphan_runtime_rows', privacy.compare(left, right)['errors'])

    def test_old_snapshots_cannot_prove_profile_erasure(self):
        legacy = fixture()
        legacy['schema_version'] = 1
        for owner in legacy['owners']:
            owner.pop('unsafe_profiles')
        for left, right in ((legacy, legacy), (legacy, fixture()), (fixture(), legacy)):
            report = privacy.compare(left, right)
            self.assertEqual(report['assessment'], 'incomplete')
            self.assertEqual(report['errors'], ['invalid_snapshot'])

    def test_matching_clean_privacy_and_empty_databases(self):
        for epoch in (1, 2, 3):
            source = fixture(epoch)
            self.assertEqual(privacy.compare(source, copy.deepcopy(source))['assessment'], 'privacy_matches_reference')
        source['owners'] = []
        self.assertEqual(privacy.compare(source, source)['assessment'], 'privacy_matches_reference')

    def test_backup_before_forget_is_blocked(self):
        report = privacy.compare(fixture(2), fixture(1))
        self.assertEqual(report['assessment'], 'blocked')
        self.assertIn('epochs_match', report['errors'])
        self.assertIn('privacy_events_match', report['errors'])

    def test_owners_are_matched_by_tenant_and_subject_not_list_order(self):
        source = fixture(1)
        other = copy.deepcopy(source['owners'][0])
        other['tenant'] = 'another-tenant'
        source['owners'].append(other)
        target = copy.deepcopy(source)
        target['owners'].reverse()
        self.assertEqual(privacy.compare(source, target)['assessment'], 'privacy_matches_reference')
        target['owners'][0]['subject'] = 'changed-subject'
        self.assertIn('owner_sets_match', privacy.compare(source, target)['errors'])

    def test_missing_and_extra_owners_fail_closed(self):
        source = fixture()
        empty = fixture()
        empty['owners'] = []
        for left, right in ((source, empty), (empty, source)):
            self.assertEqual(privacy.compare(left, right)['assessment'], 'blocked')

    def test_same_epoch_does_not_hide_missing_or_different_audit(self):
        for mutation in ('missing', 'id', 'at'):
            source, target = fixture(), fixture()
            if mutation == 'missing': target['owners'][0]['events'] = []
            elif mutation == 'id': target['owners'][0]['events'][1]['id'] += 5
            else: target['owners'][0]['events'][0]['at'] = '2026-10-14T01:00:00Z'
            self.assertEqual(privacy.compare(source, target)['assessment'], 'blocked')

    def test_bad_reference_cannot_certify_itself(self):
        for mutation in ('epoch', 'forget', 'purge', 'order', 'future'):
            source = fixture(3)
            row = source['owners'][0]
            if mutation == 'epoch': row['epoch'] += 1
            elif mutation == 'forget': row['events'].pop(0)
            elif mutation == 'purge': row['events'].pop()
            elif mutation == 'order': row['events'][0]['action'], row['events'][1]['action'] = 'purge_complete', 'forget'
            else: row['events'][1]['epoch'] = 4
            self.assertEqual(privacy.compare(source, source)['assessment'], 'blocked', mutation)

    def test_coalesced_purge_is_allowed_only_with_latest_completion(self):
        source = fixture(3)
        source['owners'][0]['events'].pop(1)
        self.assertEqual(privacy.compare(source, source)['assessment'], 'privacy_matches_reference')

    def test_pending_cleanup_or_orphans_in_either_database_block(self):
        for key in ('pending', 'orphan'):
            source, dirty = fixture(), fixture()
            if key == 'pending': dirty['owners'][0]['purge_pending'] = True
            else: dirty['orphan_owners'] = 1
            for left, right in ((source, dirty), (dirty, source)):
                self.assertEqual(privacy.compare(left, right)['assessment'], 'blocked')

    def test_each_unsafe_memory_category_blocks_both_sides(self):
        for key in privacy.COUNTERS:
            source, dirty = fixture(), fixture()
            dirty['owners'][0][key] = 1
            for left, right in ((source, dirty), (dirty, source)):
                self.assertIn('no_unsafe_memory_rows', privacy.compare(left, right)['errors'])

    def test_malformed_types_duplicate_identity_and_audit_ids_are_incomplete(self):
        mutations = (
            lambda d: d.update(schema_version=True), lambda d: d.update(owners={}),
            lambda d: d.update(schema_version=1),
            lambda d: d.update(scope='current_owner'), lambda d: d.update(captured_at='yesterday'),
            lambda d: d['owners'][0].update(epoch=True), lambda d: d['owners'][0].update(unsafe_jobs=-1),
            lambda d: d['owners'].append(copy.deepcopy(d['owners'][0])),
            lambda d: d['owners'][0]['events'][1].update(id=4),
            lambda d: d['owners'][0].update(content='secret body'),
        )
        for mutate in mutations:
            broken = fixture()
            mutate(broken)
            report = privacy.compare(fixture(), broken)
            self.assertEqual(report['assessment'], 'incomplete')
            self.assertEqual(report['errors'], ['invalid_snapshot'])

    def test_reports_do_not_export_identity_audit_details_or_content(self):
        report = privacy.compare(fixture(2), fixture(1))
        encoded = json.dumps(report)
        for text in ('private-tenant', 'private-subject', AT, 'forget', 'purge_complete'):
            self.assertNotIn(text, encoded)

    def test_cli_exit_codes_private_files_hashes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            reference, restored = folder / 'reference.json', folder / 'restored.json'
            reference.write_text(json.dumps(fixture()))
            for expected, data in ((0, fixture()), (2, fixture(1)), (3, {'wrong': 'secret-value'})):
                restored.write_text(json.dumps(data))
                output = folder / f'report-{expected}.json'
                command = [sys.executable, str(Path(privacy.__file__)), '--reference', str(reference),
                           '--restored', str(restored), '--output', str(output)]
                run = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, expected, run.stderr)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                report = json.loads(output.read_text())
                self.assertEqual(report['input_sha256']['reference'], hashlib.sha256(reference.read_bytes()).hexdigest())
                self.assertNotIn('secret-value', output.read_text() + run.stdout + run.stderr)
                before = output.read_bytes()
                self.assertEqual(subprocess.run(command, capture_output=True, timeout=10).returncode, 1)
                self.assertEqual(output.read_bytes(), before)

    def test_duplicate_json_invalid_json_and_size_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            for data in (b'{"a":1,"a":2}', b'NaN', b'[' * 2000, b'x' * (privacy.MAX_BYTES + 1)):
                path.write_bytes(data)
                with self.assertRaises((privacy.files.Invalid, ValueError, RecursionError)):
                    privacy.read(path)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RestorePrivacyTest))
    root = Path(__file__).resolve().parents[1]
    out = root / 'target/m5e/privacy-tests.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    names = ('scripts/check-restore-privacy.py', 'scripts/test-restore-privacy.py',
             'scripts/inspect-drain.py', 'ops/restore-privacy-snapshot.sql')
    out.write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped),
        'source_sha256': {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names},
        'scope': 'offline synthetic snapshots and local CLI; SQL execution is validated separately in Compose'}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
