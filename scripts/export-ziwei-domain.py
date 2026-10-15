#!/usr/bin/env python3
"""Export synthetic domain facts from the pinned Python service; never used by Java runtime."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--python-root', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('target/m6/domain'))
args = parser.parse_args()
source = args.python_root.resolve()
root = Path(__file__).resolve().parents[1]
old = json.loads((root / 'src/test/resources/ziwei/reference/manifest.json').read_text())
for name, expected in old['source_sha256'].items():
    if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('Legacy reference changed: ' + name)
service_name = 'financeclaw/agent_server/domains/ziwei/service.py'
sources = {**old['source_sha256'], service_name: hashlib.sha256((source / service_name).read_bytes()).hexdigest()}
domain_manifest = root / 'src/test/resources/ziwei/domain/manifest.json'
if domain_manifest.exists() and json.loads(domain_manifest.read_text())['source_sha256'] != sources:
    raise SystemExit('Legacy domain source changed; review before replacing frozen fixtures')
versions = {name: importlib.metadata.version(name) for name in ('x-iztro', 'tzdata', 'pydantic')}
if versions != {'x-iztro': '0.4.0', 'tzdata': '2026.3', 'pydantic': '2.13.5'}:
    raise SystemExit('Wrong legacy reference dependencies')
sys.path.insert(0, str(source))
from datetime import date, datetime
from financeclaw.kernel.ziwei import BirthContext, ZiweiConvention, ChartLevel, ResolvedTarget
from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine
from financeclaw.agent_server.domains.ziwei.service import ZiweiCalculationService, project
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError

convention = ZiweiConvention()
birth = BirthContext(solar_date=date(1990, 6, 15), shichen='yin', sex_for_chart='male',
                     timezone='Asia/Shanghai', time_precision='shichen', fingerprint='synthetic',
                     owner_fingerprint='synthetic', key_version='test', convention_ref=convention.ref)
service = ZiweiCalculationService(XIztroEngine(), convention)
cases = []
for level, start, end in [
    ('natal', None, None), ('decadal', '2026-01-01', '2027-01-01'),
    ('yearly', '2026-01-01', '2027-01-01'), ('monthly', '2026-02-01', '2026-03-01'),
    ('daily', '2026-02-16', '2026-02-19'), ('daily', '2026-01-01', '2026-02-02'),
    ('daily', '1989-01-01', '1989-01-02'), ('yearly', '1990-06-15', '1990-06-16')]:
    case = {'level': level, 'start': start, 'end': end}
    target = None if start is None else ResolvedTarget(start=date.fromisoformat(start),
        end=date.fromisoformat(end), timezone='Asia/Shanghai', request_clock=datetime.fromisoformat('2026-01-01T00:00:00+08:00'))
    try:
        result = service.calculate_snapshot(birth, target, ChartLevel(level))
        case['facts'] = [{'fact_id': f.fact_id, 'layer': f.layer, 'palace_index': f.palace_index,
                          'value': json.loads(f.value_json)} for f in result.facts]
        case['segments'] = [s.model_dump(mode='json') for s in result.segments]
        case['projections'] = {}
        for focus in ('overall', 'career', 'relationship', 'wealth'):
            # Compare all selected evidence independently of the Java envelope byte budget.
            p = project(result, focus, max_bytes=1000000)
            case['projections'][focus] = [f.fact_id for f in p.facts]
    except ZiweiError as error:
        case['error'] = error.code
    cases.append(case)
args.output.mkdir(parents=True, exist_ok=True)
payload = (json.dumps(cases, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
(args.output / 'domain.json').write_bytes(payload)
(args.output / 'manifest.json').write_text(json.dumps({'cases': len(cases), 'dependencies': versions,
    'source_sha256': sources, 'fixture_sha256': hashlib.sha256(payload).hexdigest(),
    'purpose': 'Synthetic facts/segments/focus parity. Java convention and ownership intentionally differ.'}, indent=2) + '\n')
print(json.dumps({'cases': len(cases), 'sha256': hashlib.sha256(payload).hexdigest()}))
