#!/usr/bin/env python3
"""Freeze an expanded synthetic full-chart corpus from the exact legacy packages and source."""
import argparse
from datetime import date, timedelta
import gzip
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--python-root', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('target/m6/chart-reference'))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
baseline = json.loads((root / 'src/test/resources/ziwei/reference/manifest.json').read_text())
if any(metadata.version(name) != version for name, version in baseline['packages'].items()):
    raise SystemExit('Use the exact interpreter packages in the frozen M6 manifest')
for name, digest in baseline['source_sha256'].items():
    if hashlib.sha256((args.python_root / name).read_bytes()).hexdigest() != digest:
        raise SystemExit('Legacy source differs from frozen M6 reference')
sys.path.insert(0, str(args.python_root.resolve()))
from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine, SHICHEN
from financeclaw.kernel.ziwei import BirthContext, BirthInput, ZiweiConvention

engine, convention = XIztroEngine(), ZiweiConvention()
inputs = []

def add(group, solar, slot, sex, target, calendar='solar', lunar=None):
    ymd = [solar.year, solar.month, solar.day] if lunar is None else lunar[:3]
    inputs.append((group, {'calendar': calendar, 'date': ymd,
                          'is_leap_month': bool(lunar[3]) if lunar else False,
                          'shichen': slot, 'sex_for_chart': sex, 'target': target.isoformat()}))

# Every sex/time pair for all sixty birth-year stem/branch combinations.
for year in range(1901, 1961):
    for index, slot in enumerate(SHICHEN):
        for sex in ('male', 'female'):
            solar = date(year, 3 + (year - 1901) % 10, 1 + (year * 7 + index) % 28)
            target = date(2000 + year - 1901, 3 + index % 10, 1 + (year + index) % 28)
            add('cycle-sex-time', solar, slot, sex, target)

# Every corrected boundary, lunar leap half, calendar year, solar term and lunar month end.
edges = {date(1901, 1, 1), date(2099, 12, 31), date(2000, 2, 29)}
for center in ('1925-05-22', '1925-06-20', '2057-09-28', '2057-10-27',
               '2023-04-05', '2023-04-06', '2026-02-04', '2026-02-17',
               '2027-01-01', '2000-12-31'):
    edges.update(date.fromisoformat(center) + timedelta(days=offset) for offset in (-1, 0, 1))
# Find real month ends from the independent frozen calendar export, not the Java implementation.
calendar_path = root / 'src/test/resources/ziwei/calendar/calendar.tsv.gz'
calendar_manifest = json.loads((calendar_path.parent / 'calendar-manifest.json').read_text())
if hashlib.sha256(calendar_path.read_bytes()).hexdigest() != calendar_manifest['gzip_sha256']:
    raise SystemExit('Frozen calendar digest mismatch')
calendar_rows = [line.split('\t') for line in gzip.decompress(calendar_path.read_bytes()).decode().splitlines()]
for current, following in zip(calendar_rows, calendar_rows[1:]):
    if current[0].startswith(('1925-', '2000-', '2023-', '2057-')) and following[3] == '1':
        edges.add(date.fromisoformat(current[0]))
        edges.add(date.fromisoformat(following[0]))
for solar in sorted(edges):
    for slot in ('zi_early', 'zi_late'):
        for sex in ('male', 'female'):
            add('calendar-boundaries', solar, slot, sex, solar)

# Explicit lunar input, normal and leap months, both halves and month ends.
for year, month, day, leap in ((1925,4,30,False),(1925,4,29,True),(2057,9,30,False),
                              (2023,2,15,True),(2023,2,16,True),(2023,2,29,True),(2000,7,17,False)):
    solar = engine.solar_date(BirthInput.model_validate({'calendar':'lunar',
        'date':{'year':year,'month':month,'day':day},'is_leap_month':leap}))
    for slot in SHICHEN:
        for sex in ('male','female'):
            add('lunar-input',solar,slot,sex,date(2026,2,17),'lunar',[year,month,day,leap])

# All nominal ages through the legacy fallback above the final decadal/age table, plus pre-birth.
for age in range(1, 141):
    for sex in ('male', 'female'):
        add('age-periods',date(1940,7,17),'yin',sex,date(1940+age-1,7,17))
add('pre-birth',date(2099,12,31),'zi_late','female',date(1901,1,1))

args.output.mkdir(parents=True, exist_ok=True)
raw_digest = hashlib.sha256()
groups = {}
coverage = {'year_pillars':set(), 'element_classes':set(), 'times':set(), 'sexes':set(), 'target_year_pillars':set()}
path = args.output / 'charts.jsonl.gz'
with path.open('wb') as destination, gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as output:
    for index, (group, data) in enumerate(inputs):
        birth = BirthInput.model_validate({'calendar':data['calendar'], 'date':dict(zip(('year','month','day'),data['date'])),
                                          'is_leap_month':data['is_leap_month']})
        solar = engine.solar_date(birth)
        context = BirthContext(solar_date=solar,shichen=data['shichen'],sex_for_chart=data['sex_for_chart'],
            timezone='Asia/Shanghai',time_precision='shichen',fingerprint='synthetic',owner_fingerprint='synthetic',
            key_version='synthetic',convention_ref=convention.ref)
        natal = engine.natal(context,convention)
        horoscope = engine.horoscope(context,convention,date.fromisoformat(data['target']))
        row = {'id':f'{group}-{index:04d}', 'input':data, 'solar_date':solar.isoformat(),'natal':natal,'horoscope':horoscope}
        payload = (json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
        raw_digest.update(payload)
        output.write(payload)
        groups[group] = groups.get(group,0)+1
        coverage['year_pillars'].add(tuple(natal['rawDates']['chineseDate']['yearlyKeys']))
        coverage['element_classes'].add(natal['fiveElementsClassKey'])
        coverage['times'].add(data['shichen'])
        coverage['sexes'].add(data['sex_for_chart'])
        coverage['target_year_pillars'].add((horoscope['yearly']['heavenlyStemKey'],horoscope['yearly']['earthlyBranchKey']))
        if (index+1)%500==0: print('Frozen full charts:',index+1,flush=True)
manifest = {'reference':'x-iztro@0.4.0', 'scope':'Synthetic fixed-rule raw chart compatibility, not independent astrology correctness',
    'source_commit':baseline['source_commit'],'source_sha256':baseline['source_sha256'],'packages':baseline['packages'],
    'convention':baseline['convention'],'cases':len(inputs),'groups':groups,
    'coverage':{key:sorted(value) for key,value in coverage.items()},
    'jsonl_sha256':raw_digest.hexdigest(),'gzip_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
(args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'cases':len(inputs),'groups':groups,'output':str(args.output)},ensure_ascii=False),flush=True)
