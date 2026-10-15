#!/usr/bin/env python3
"""Compare a candidate engine's full JSON output to M6 references; never runs an engine or a model."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--candidate', type=Path, required=True,
                    help='JSON array with the same IDs, inputs and full outputs as charts.json')
parser.add_argument('--engine-ref', required=True, help='Exact candidate version/commit, recorded as provenance only')
parser.add_argument('--output', type=Path, default=Path('target/m6/chart-comparison.json'))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
reference = root / 'src/test/resources/ziwei/reference'

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result

def read(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Candidate exceeds 4 MiB')
    return json.loads(path.read_text(), object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON number')))

def indexed(value):
    if not isinstance(value, list):
        raise ValueError('Expected a JSON array')
    result = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in result:
            raise ValueError('Missing or duplicate case ID')
        result[row['id']] = row
    return result

def differences(expected, actual, path):
    if type(expected) is not type(actual):
        return [path]
    if isinstance(expected, dict):
        result = [f'{path}/{key}' for key in sorted(set(expected) ^ set(actual))]
        for key in sorted(set(expected) & set(actual)):
            result.extend(differences(expected[key], actual[key], f'{path}/{key}'))
        return result
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [path + '/length']
        return [item for index, (a, b) in enumerate(zip(expected, actual))
                for item in differences(a, b, f'{path}/{index}')]
    return [] if expected == actual else [path]

try:
    manifest = read(reference / 'manifest.json')
    expected_digest = manifest['fixture_sha256']['charts.json']
    if hashlib.sha256((reference / 'charts.json').read_bytes()).hexdigest() != expected_digest:
        raise ValueError('Reference digest mismatch')
    expected, actual = indexed(read(reference / 'charts.json')), indexed(read(args.candidate))
    if set(expected) != set(actual):
        raise ValueError('Candidate case IDs do not exactly match reference')
    mismatches = {key: differences(expected[key], actual[key], key) for key in sorted(expected)}
    mismatches = {key: paths for key, paths in mismatches.items() if paths}
    report = {'scope': 'Synthetic raw chart compatibility only; does not certify a Java engine or product enablement',
              'engine_ref': args.engine_ref, 'reference_sha256': expected_digest,
              'candidate_sha256': hashlib.sha256(args.candidate.read_bytes()).hexdigest(),
              'cases': len(expected), 'matched': len(expected) - len(mismatches),
              'mismatches': mismatches, 'success': not mismatches}
except (OSError, ValueError, KeyError) as error:
    # Never print candidate data or parser exception text.
    report = {'success': False, 'error': type(error).__name__, 'reason': 'Invalid input or baseline; verify schema, case IDs and digest'}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'success': report['success'], 'report': str(args.output)}, ensure_ascii=False))
raise SystemExit(0 if report['success'] else 1)
