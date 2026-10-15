#!/usr/bin/env python3
"""Offline contract tests for the opt-in live acceptance validator; no providers or credentials."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('live', Path(__file__).with_name('validate-live.py'))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
AID = '00000000-0000-4000-8000-000000000001'
TOOL = 'ziwei_yearly_chart'


class LiveAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.facts = list(live.reference_natal().values()) + [
            {'fact_id': 'yearly.core.2', 'layer': 'yearly', 'value': {'branch': '午'}}]
        self.target = {'start': '2026-02-17', 'end': '2026-02-18', 'timezone': 'Asia/Shanghai'}
        self.segments = [{'start': '2026-02-17', 'end': '2026-02-18', 'fact_ids': ['yearly.core.2']}]
        self.answer = {'mode': 'chart_only', 'artifact_id': AID, 'chart_id': 'synthetic',
                       'target': self.target, 'segments': [{k: self.segments[0][k] for k in ('start', 'end')}],
                       'citations': [{'fact_id': self.facts[0]['fact_id'], 'path': '/body', 'value': '火星'},
                                     {'fact_id': 'yearly.core.2', 'path': '/branch', 'value': '午'}]}
        self.pages = []

    def artifact(self, path):
        query = parse_qs(urlsplit(path).query)
        field, start = query['path'][0], int(query['start'][0])
        self.pages.append((field, start))
        values = {'/level': 'yearly', '/chart_id': 'synthetic', '/target': self.target,
                  '/segments': self.segments, '/facts': self.facts}
        value = values[field]
        # Deliberately return fewer rows than requested, as the byte budget can require.
        data = value[start:start + 1] if isinstance(value, list) else value
        return {'artifact_id': AID, 'hash': 'stable', 'start': start,
                'count': len(data) if isinstance(value, list) else 1,
                'total': len(value) if isinstance(value, list) else 1, 'data': data}

    def verify(self):
        return live.verify_ziwei(self.artifact, json.dumps(self.answer), TOOL)

    def test_correct_citations_follow_all_partial_pages(self):
        self.assertEqual(self.verify()['verified_citations'], 2)
        self.assertIn(('/facts', 1), self.pages)
        self.facts[0]['value']['body'] = 'different-birth-chart'
        with self.assertRaisesRegex(ValueError, 'Birth chart'):
            self.verify()

    def test_fabricated_id_or_value_or_pointer_is_rejected(self):
        original = copy.deepcopy(self.answer)
        for change in ({'fact_id': 'fabricated'}, {'value': '错'}, {'path': '/missing'}, {'path': 'branch'}):
            with self.subTest(change=change):
                self.answer = copy.deepcopy(original)
                self.answer['citations'][1].update(change)
                with self.assertRaises((ValueError, KeyError)):
                    self.verify()

    def test_wrong_level_or_duplicate_fact_or_free_text_is_rejected(self):
        original = copy.deepcopy(self.answer)
        mutations = [lambda a: a.update(mode='interpret'), lambda a: a.update(extra='解读'),
                     lambda a: a['citations'].__setitem__(1, copy.deepcopy(a['citations'][0])),
                     lambda a: a.update(citations=a['citations'][:1]),
                     lambda a: a.update(chart_id='guessed')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.answer = copy.deepcopy(original)
                mutate(self.answer)
                with self.assertRaises(ValueError):
                    self.verify()
        self.answer = original
        self.facts[-1]['layer'] = 'monthly'
        with self.assertRaisesRegex(ValueError, 'chart layer'):
            self.verify()

    def test_missing_or_wrong_interval_is_rejected(self):
        self.answer['segments'] = []
        with self.assertRaisesRegex(ValueError, 'segment coverage'):
            self.verify()
        self.answer['segments'] = [{'start': '2026-02-17', 'end': '2026-02-18'}]
        self.answer['target'] = {'start': 'wrong'}
        with self.assertRaisesRegex(ValueError, 'target coverage'):
            self.verify()
        self.target = self.answer['target'] = {'start': '2026-02-18', 'end': '2026-02-19'}
        with self.assertRaisesRegex(ValueError, 'synthetic request'):
            self.verify()

    def test_artifact_hash_drift_and_zero_progress_fail_closed(self):
        count = 0

        def drift(path):
            nonlocal count
            page = self.artifact(path)
            count += 1
            page['hash'] = str(count)
            return page
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            live.verify_ziwei(drift, json.dumps(self.answer), TOOL)

        def empty(path):
            page = self.artifact(path)
            if '/facts' in path or '%2Ffacts' in path:
                page.update(data=[], count=0)
            return page
        with self.assertRaisesRegex(ValueError, 'pagination'):
            live.verify_ziwei(empty, json.dumps(self.answer), TOOL)

    def fake_api(self, *, clarify=False, events=True, wrong_question=False):
        self.calls, state = [], {'answered': False}

        def call(path, body=None, method=None, raw=False):
            self.calls.append((path, body))
            if path.startswith('/v1/artifacts/'):
                return self.artifact(path)
            if path == '/v1/conversations':
                return {'conversation_id': 'synthetic-conversation'}
            if path.endswith('/grant'):
                return {'revision': 1}
            if path.endswith('/turns'):
                return {'turn_id': 'synthetic-turn'}
            if path.endswith('/events'):
                return 'data: ' + json.dumps({'kind': 'tool_completed', 'tool': TOOL}) if events else ''
            if path.endswith('/interactions'):
                return [{'id': 'question', 'kind': 'approval' if wrong_question else 'input',
                         'tool': TOOL, 'status': 'pending', 'live': True, 'revision': 1}]
            if path.endswith('/answer'):
                state['answered'] = True
                return {}
            return {'status': 'waiting' if clarify and not state['answered'] else 'completed',
                    'result': json.dumps(self.answer)}
        return call

    def execute(self, call, clarify=False):
        args = argparse.Namespace(kind='ziwei', expected_tool=TOOL, clarify=clarify, prompt=None)
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch('builtins.print'):
                    live.run(args, call, sleep=lambda _: None)
                evidence = json.loads(next(Path('target/m6').glob('live-*.json')).read_text())
                self.assertEqual(evidence['status'], 'passed')
                self.assertNotIn('命宫', json.dumps(evidence, ensure_ascii=False))
                self.assertNotIn('1990', json.dumps(evidence))
                return evidence
            finally:
                os.chdir(previous)

    def test_clarification_uses_existing_turn_and_only_fixed_birth_answer(self):
        evidence = self.execute(self.fake_api(clarify=True), clarify=True)
        self.assertTrue(evidence['clarification_resumed'])
        self.assertEqual(sum(p.endswith('/turns') for p, _ in self.calls), 1)
        answer = next(body for p, body in self.calls if p.endswith('/answer'))
        self.assertEqual(answer['answer'], {'birth': {'sex_for_chart': 'male'}})
        grant = next(body for p, body in self.calls if p.endswith('/grant') and body)
        self.assertEqual(grant['scopes'], ['ziwei:read', 'artifact:read'])

    def test_text_only_or_guessed_missing_input_is_not_acceptance(self):
        with self.assertRaisesRegex(ValueError, 'tool event'):
            self.execute(self.fake_api(events=False))
        with self.assertRaisesRegex(ValueError, 'guessed'):
            self.execute(self.fake_api(), clarify=True)

    def test_unexpected_approval_is_never_answered(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected pending'):
            self.execute(self.fake_api(clarify=True, wrong_question=True), clarify=True)
        self.assertFalse(any(p.endswith('/answer') for p, _ in self.calls))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LiveAcceptanceTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parents[1] / 'target/m6/live-validator-tests.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
                                 'errors': len(result.errors), 'skipped': len(result.skipped),
                                 'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                   for p in (Path(__file__), Path(__file__).with_name('validate-live.py'))},
                                 'scope': 'Offline validator contracts; no real model or remote API'}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
