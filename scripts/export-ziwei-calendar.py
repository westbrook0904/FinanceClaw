#!/usr/bin/env python3
"""Export exhaustive synthetic calendar dates and selected TZDB transitions from M6's frozen source."""
import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--python-root', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('target/m6/calendar-reference'))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
baseline = json.loads((root / 'src/test/resources/ziwei/reference/manifest.json').read_text())
if any(metadata.version(name) != version for name, version in baseline['packages'].items()):
    raise SystemExit('Use the exact interpreter package versions in the frozen M6 manifest')
for name, digest in baseline['source_sha256'].items():
    if hashlib.sha256((args.python_root / name).read_bytes()).hexdigest() != digest:
        raise SystemExit('Legacy source differs from the frozen M6 reference')
sys.path.insert(0, str(args.python_root.resolve()))
from x_iztro import Astro
from pydantic import ValidationError
from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.kernel.ziwei import BirthInput

engine = XIztroEngine()
cursor, stop = date(1901, 1, 1), date(2100, 1, 1)
rows = []
while cursor < stop:
    raw = Astro().by_solar(cursor.isoformat(), 0, 'male').raw_dates.lunar_date
    rows.append(f'{cursor.isoformat()}\t{raw.lunar_year}\t{raw.lunar_month}\t{raw.lunar_day}\t{int(raw.is_leap)}\n')
    cursor += timedelta(days=1)
    if cursor.month == 1 and cursor.day == 1 and cursor.year % 25 == 0:
        print(f'Calendar reference through {cursor.year - 1}: {len(rows)} days', flush=True)
payload = ''.join(rows).encode()

cases = []
inputs = [
    ('solar', 2000, 2, 29, None), ('solar', 1900, 1, 1, None),
    ('solar', 2100, 1, 1, None), ('solar', 2026, 2, 29, None),
    ('solar', 2000, 8, 16, True), ('solar', 2000, 8, 16, False),
    ('lunar', 2000, 7, 17, False), ('lunar', 2000, 7, 17, True),
    ('lunar', 2023, 2, 15, True), ('lunar', 2023, 2, 16, True),
    ('lunar', 2023, 2, 29, True), ('lunar', 2023, 2, 30, True),
    ('lunar', 2023, 2, 30, False), ('lunar', 2099, 12, 30, False),
    ('lunar', 1901, 1, 1, False), ('lunar', 2026, 1, 0, False),
    ('lunar', 1925, 4, 29, False), ('lunar', 1925, 4, 30, False),
    ('lunar', 1925, 4, 29, True), ('lunar', 1925, 4, 30, True),
    ('lunar', 2057, 8, 29, False), ('lunar', 2057, 8, 30, False),
    ('lunar', 2057, 9, 29, False), ('lunar', 2057, 9, 30, False),
]
for calendar, year, month, day, leap in inputs:
    case = {'calendar': calendar, 'year': year, 'month': month, 'day': day, 'leap': leap}
    try:
        solar = engine.solar_date(BirthInput.model_validate({
            'calendar': calendar, 'date': {'year': year, 'month': month, 'day': day}, 'is_leap_month': leap}))
        if not 1901 <= solar.year <= 2099:
            raise ValueError('Outside domain range')
        case['solar_date'] = solar.isoformat()
    except (ValidationError, ZiweiError, ValueError):
        case['rejected'] = True
    cases.append(case)

# Observe every UTC day, bisect each changed offset to its exact second, then record both sides.
# This scope covers these explicit zones; it is not a claim that all IANA zones are compatible.
zones = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Taipei', 'America/New_York',
         'Pacific/Auckland', 'Europe/London', 'Australia/Lord_Howe', 'Pacific/Apia', 'UTC']
start_epoch = int(datetime(1901, 1, 1, tzinfo=timezone.utc).timestamp())
end_epoch = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp())
zone_cases = []
for name in zones:
    zone = engine.zone(name)
    def offset(epoch):
        return int(datetime.fromtimestamp(epoch, timezone.utc).astimezone(zone).utcoffset().total_seconds())
    instants = {start_epoch, end_epoch - 1}
    previous = offset(start_epoch)
    for epoch in range(start_epoch + 86400, end_epoch, 86400):
        current = offset(epoch)
        if current != previous:
            low, high = epoch - 86400, epoch
            while high - low > 1:
                middle = (low + high) // 2
                if offset(middle) == previous:
                    low = middle
                else:
                    high = middle
            instants.update((high - 1, high, high + 1))
        previous = current
    zone_cases.append({'zone': name, 'samples': [[epoch, offset(epoch)] for epoch in sorted(instants)]})

compressed = gzip.compress(payload, mtime=0)
manifest = {'scope': 'Calendar conversion only; full charts and all-IANA timezone compatibility are not certified',
            'reference': 'x-iztro@0.4.0', 'tzdata': baseline['packages']['tzdata'],
            'source_commit': baseline['source_commit'], 'source_sha256': baseline['source_sha256'],
            'packages': baseline['packages'], 'first': '1901-01-01', 'end_exclusive': '2100-01-01',
            'days': len(rows), 'columns': ['solar_date', 'lunar_year', 'lunar_month', 'lunar_day', 'is_leap'],
            'tsv_sha256': hashlib.sha256(payload).hexdigest(),
            'gzip_sha256': hashlib.sha256(compressed).hexdigest(), 'birth_cases': cases, 'timezones': zone_cases}
args.output.mkdir(parents=True, exist_ok=True)
(args.output / 'calendar.tsv.gz').write_bytes(compressed)
(args.output / 'calendar-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'days': len(rows), 'birth_cases': len(cases),
                  'timezone_samples': sum(len(z['samples']) for z in zone_cases),
                  'output': str(args.output)}, ensure_ascii=False), flush=True)
