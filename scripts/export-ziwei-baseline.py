#!/usr/bin/env python3
"""Export synthetic M6 reference cases from the frozen legacy implementation, never a runtime bridge."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--python-root', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('target/m6/reference'))
args = parser.parse_args()
source = args.python_root.resolve()
root = Path(__file__).resolve().parents[1]
versions = {name: importlib.metadata.version(name) for name in ('x-iztro', 'tzdata', 'pydantic')}
if versions != {'x-iztro': '0.4.0', 'tzdata': '2026.3', 'pydantic': '2.13.5'}:
    raise SystemExit('Reference interpreter must use x-iztro 0.4.0, tzdata 2026.3 and pydantic 2.13.5')
files = ['financeclaw/kernel/ziwei.py', 'financeclaw/kernel/ziwei_tools.py',
         'financeclaw/kernel/context.py',
         *[f'financeclaw/agent_server/domains/ziwei/{name}.py' for name in
           ('normalization', 'validation', 'errors', 'tool_inputs', 'adapters/x_iztro')]]
hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
frozen = root / 'src/test/resources/ziwei/reference/manifest.json'
if frozen.exists() and json.loads(frozen.read_text())['source_sha256'] != hashes:
    raise SystemExit('Legacy source differs from frozen M6 baseline; review changes before replacing fixtures')
sys.path.insert(0, str(source))
from pydantic import ValidationError
from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.domains.ziwei.normalization import _birth_time, resolve_target
from financeclaw.agent_server.domains.ziwei.tool_inputs import analysis_request
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.ziwei import BirthContext, BirthInput, ZiweiConvention
from financeclaw.kernel.ziwei_tools import ZIWEI_TOOL_INPUTS
from datetime import date

engine = XIztroEngine()
clock = '2026-01-01T00:30:00+08:00'
zones = ('Asia/Shanghai', 'America/New_York', 'Pacific/Auckland')
targets = []

def target_case(name, arguments, zone='Asia/Shanghai', request_clock=clock):
    case = {'id': f'target-{len(targets):03d}', 'tool': name, 'arguments': arguments,
            'request_clock': request_clock, 'timezone': zone}
    try:
        context = ExecutionContext(tenant_id='synthetic', subject_id='synthetic', turn_id='synthetic',
                                   request_clock=request_clock, timezone=zone)
        result = resolve_target(analysis_request(name, arguments), context, engine)
        case['expected'] = None if result is None else {'start': result.start.isoformat(), 'end': result.end.isoformat()}
    except (ValidationError, ZiweiError) as error:
        case['error'] = error.code if isinstance(error, ZiweiError) else 'ZIWEI_TOOL_INPUT_INVALID'
    targets.append(case)

for tool in ZIWEI_TOOL_INPUTS:
    target_case(tool, {})
    target_case(tool, {'on_date': '2026-02-17'})
    target_case(tool, {'date_range': {'start': '2024-01-01', 'end': '2025-01-01'}})
    for extra in ('level', 'target', 'tenant_id', 'request_clock'):
        target_case(tool, {extra: 'not-model-controlled'})
    if tool == 'ziwei_natal_chart':
        continue
    offset = 'year_offset' if 'yearly' in tool else 'month_offset' if 'monthly' in tool else 'day_offset'
    for zone in zones:
        for amount in (-1, 0, 1):
            target_case(tool, {offset: amount}, zone)
    target_case(tool, {offset: 0}, request_clock=None)
    target_case(tool, {offset: 11})
    target_case(tool, {offset: 0, 'on_date': '2026-02-17'})
    for start, end in [('2026-02-17', '2026-02-17'), ('2026-02-18', '2026-02-17'),
                       ('2024-01-01', '2025-01-02'), ('1900-12-31', '1901-01-01'),
                       ('2099-12-31', '2100-01-01')]:
        target_case(tool, {'date_range': {'start': start, 'end': end}})
    for value in ('2026-02-30', '1900-12-31', '2099-12-31'):
        target_case(tool, {'on_date': value})
target_case('ziwei_yearly_chart', {'year': 2024})
target_case('ziwei_yearly_chart', {'year': 2099})
target_case('ziwei_monthly_chart', {'year': 2024, 'month': 2})
target_case('ziwei_monthly_chart', {'year': 2026})
target_case('ziwei_monthly_chart', {'month': 2})
target_case('ziwei_monthly_chart', {'year': 2026, 'month': 13})

times = []

def time_case(day, zone, value):
    case = {'id': f'time-{len(times):03d}', 'solar_date': day, 'timezone': zone, 'input': value}
    try:
        birth = BirthInput.model_validate({'time': value})
        if birth.time.kind == 'unknown':
            raise ZiweiError('ZIWEI_INPUT_INCOMPLETE', 'missing', ('birth.time',))
        slot, instant = _birth_time(birth, date.fromisoformat(day), engine.zone(zone), [])
        case['expected'] = {'shichen': slot, 'utc_instant': instant.isoformat() if instant else None,
                            'precision': birth.time.kind}
    except (ValidationError, ZiweiError) as error:
        case['error'] = error.code if isinstance(error, ZiweiError) else 'ZIWEI_TOOL_INPUT_INVALID'
    times.append(case)

for hour in ('00:00', '00:59', '01:00', '02:59', '03:00', '22:59', '23:00', '23:59'):
    time_case('2000-08-16', 'Asia/Shanghai', {'kind': 'clock', 'clock': hour})
for day, hour in [('2024-03-10', '02:30'), ('2024-11-03', '01:30'), ('1991-04-14', '02:30')]:
    for fold in (None, 0, 1):
        time_case(day, 'Asia/Shanghai' if day.startswith('1991') else 'America/New_York',
                  {'kind': 'clock', 'clock': hour, 'fold': fold})
for value in [{'kind': 'unknown'}, {'kind': 'shichen', 'shichen': 'zi'},
              {'kind': 'shichen', 'shichen': 'yin'}, {'kind': 'shichen', 'shichen': 'zi_late'},
              {'kind': 'range', 'clock': '03:10', 'end': '04:50'},
              {'kind': 'range', 'clock': '04:50', 'end': '05:10'},
              {'kind': 'range', 'clock': '23:50', 'end': '00:10'},
              {'kind': 'clock', 'clock': '03:00', 'shichen': 'yin'}]:
    time_case('2000-08-16', 'Asia/Shanghai', value)
time_case('2024-03-10', 'America/New_York', {'kind': 'range', 'clock': '01:30', 'end': '03:30'})

# Full native output, including all horoscope ancestors. These are compatibility references,
# not independently certified astrological truth. Every date is synthetic/public sample data.
charts = []
samples = [
    ('solar-female', 'solar', (2000, 8, 16), False, 'yin', 'female', '2026-02-16'),
    ('solar-male', 'solar', (2000, 8, 16), False, 'yin', 'male', '2026-02-17'),
    ('lunar-equivalent', 'lunar', (2000, 7, 17), False, 'yin', 'female', '2026-02-16'),
    ('late-zi', 'solar', (2000, 8, 16), False, 'zi_late', 'female', '2026-02-17'),
    ('next-early-zi', 'solar', (2000, 8, 17), False, 'zi_early', 'female', '2026-02-17'),
    ('leap-first-half', 'lunar', (2023, 2, 15), True, 'chou', 'male', '2026-12-31'),
    ('leap-second-half', 'lunar', (2023, 2, 16), True, 'chou', 'male', '2027-01-01'),
    ('minimum-year', 'solar', (1901, 1, 1), False, 'zi_early', 'male', '1901-01-02'),
    ('maximum-year', 'solar', (2099, 12, 31), False, 'hai', 'female', '2099-12-31'),
]
for name, calendar, ymd, leap, slot, sex, target in samples:
    birth = BirthInput.model_validate({'calendar': calendar, 'date': dict(zip(('year', 'month', 'day'), ymd)),
                                      'is_leap_month': leap})
    solar = engine.solar_date(birth)
    context = BirthContext(solar_date=solar, shichen=slot, sex_for_chart=sex, timezone='Asia/Shanghai',
                           time_precision='shichen', fingerprint='synthetic', owner_fingerprint='synthetic',
                           key_version='synthetic', convention_ref=ZiweiConvention().ref)
    charts.append({'id': name, 'input': {'calendar': calendar, 'date': list(ymd), 'is_leap_month': leap,
                                       'shichen': slot, 'sex_for_chart': sex, 'target': target},
                   'solar_date': solar.isoformat(), 'natal': engine.natal(context, ZiweiConvention()),
                   'horoscope': engine.horoscope(context, ZiweiConvention(), date.fromisoformat(target))})

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()

outputs = {'dates.json': encoded({'targets': targets, 'civil_times': times}),
           'schemas.json': encoded({name: model.model_json_schema() for name, model in ZIWEI_TOOL_INPUTS.items()}),
           'charts.json': encoded(charts)}
manifest = {'purpose': 'Synthetic compatibility baseline, not an independent algorithm certification',
            'source_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
            'source_sha256': hashes, 'packages': versions, 'convention': ZiweiConvention().model_dump(mode='json'),
            'fixture_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in outputs.items()},
            'cases': {'targets': len(targets), 'civil_times': len(times), 'charts': len(charts)}}
outputs['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
args.output.mkdir(parents=True, exist_ok=True)
for name, data in outputs.items():
    (args.output / name).write_bytes(data)
print(json.dumps({'output': str(args.output), 'cases': manifest['cases']}, ensure_ascii=False))
