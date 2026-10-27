#!/usr/bin/env python3
"""Evidence boundary tests only; real outage/restart acceptance still requires Compose."""
import ast
from copy import deepcopy
from pathlib import Path
import unittest


# validate-compose intentionally executes containers at top level. Load only the pure evidence
# predicates so these checks cannot parse its CLI, read secrets or start a deployment.
source = Path(__file__).with_name('validate-compose.py')
tree = ast.parse(source.read_text(), filename=str(source))
names = {'assert_expired_worker_unchanged', 'assert_recovered_within_original_ttl',
         'completed_recovery_read'}
selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert len(selected) == len(names)
predicates = {}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), predicates)
recovered = predicates['assert_recovered_within_original_ttl']
expired = predicates['assert_expired_worker_unchanged']
completed_read = predicates['completed_recovery_read']


class RecoveryEvidence(unittest.TestCase):
    def setUp(self):
        self.before = {'worker_id': 'same-worker', 'ttl_ms': 60000, 'state': 'active',
                       'live': True, 'database_epoch_ms': 101000,
                       'heartbeat_epoch_ms': 100000, 'expires_epoch_ms': 160000}
        self.after = {**self.before, 'database_epoch_ms': 130000,
                      'heartbeat_epoch_ms': 129000, 'expires_epoch_ms': 189000}
        self.container = {'container_id': 'same-container', 'started_at': 'original-start'}

    def test_same_process_recovers_before_the_original_deadline(self):
        recovered(self.before, self.after, self.container, deepcopy(self.container))

    def test_new_future_expiry_cannot_hide_an_expired_original_deadline(self):
        for timestamp in (160000, 170000):
            with self.subTest(database_epoch_ms=timestamp), self.assertRaises(AssertionError):
                recovered(self.before, {**self.after, 'database_epoch_ms': timestamp},
                          self.container, self.container)

    def test_restarted_container_cannot_count_as_automatic_recovery(self):
        for change in ({'container_id': 'replacement'}, {'started_at': 'later-start'}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                recovered(self.before, self.after, self.container, {**self.container, **change})

    def test_changed_uuid_or_ttl_cannot_count_as_the_same_registration(self):
        for change in ({'worker_id': 'replacement'}, {'ttl_ms': 15000}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                recovered(self.before, {**self.after, **change}, self.container, self.container)

    def test_readiness_without_a_fresh_heartbeat_is_insufficient(self):
        stale = {**self.before, 'database_epoch_ms': 130000}
        with self.assertRaises(AssertionError):
            recovered(self.before, stale, self.container, self.container)

    def test_expired_identity_stays_inactive_while_pending_task_waits(self):
        frozen = {**self.before, 'live': False, 'database_epoch_ms': 170000}
        expired(frozen, {**frozen, 'database_epoch_ms': 180000})

    def test_any_renewal_of_expired_identity_fails_even_if_it_expires_again(self):
        frozen = {**self.before, 'live': False, 'database_epoch_ms': 170000}
        for change in ({'heartbeat_epoch_ms': 101000}, {'expires_epoch_ms': 165000}, {'live': True}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                expired(frozen, {**frozen, 'database_epoch_ms': 180000, **change})


class BusinessHttpRecoveryEvidence(unittest.TestCase):
    def test_completed_read_within_original_deadline_succeeds(self):
        self.assertTrue(completed_read(200, {'status': 'completed'}, 44.9, 45))

    def test_only_transport_failure_and_unavailable_status_may_wait(self):
        for status in (0, 503):
            with self.subTest(status=status):
                self.assertFalse(completed_read(status, None, 3, 45))

    def test_other_http_errors_fail_immediately(self):
        for status in (400, 401, 403, 404, 409, 422, 429, 500, 502, 504):
            with self.subTest(status=status), self.assertRaises(AssertionError):
                completed_read(status, None, 3, 45)

    def test_other_success_statuses_do_not_count_as_a_completed_read(self):
        for status in (201, 202, 204):
            with self.subTest(status=status), self.assertRaises(AssertionError):
                completed_read(status, {'status': 'completed'}, 3, 45)

    def test_wrong_or_missing_durable_terminal_state_fails_immediately(self):
        for body in (None, [], 'completed', {}, {'status': None}, {'status': 'queued'},
                     {'status': 'running'}, {'status': 'failed'}, {'status': 'blocked'}):
            with self.subTest(body=body), self.assertRaises(AssertionError):
                completed_read(200, body, 3, 45)

    def test_deadline_rejects_both_late_success_and_further_waiting(self):
        for elapsed in (45, 45.1):
            for status in (0, 200, 503):
                with self.subTest(elapsed=elapsed, status=status), self.assertRaises(AssertionError):
                    completed_read(status, {'status': 'completed'}, elapsed, 45)


if __name__ == '__main__':
    unittest.main()
