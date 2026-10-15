#!/usr/bin/env python3
"""Regenerate constant Java star tables from the locked MIT x-iztro source, never chart fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('target/m6/ZiweiTables.java'))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
lock = json.loads((root / 'ziwei-dependencies.lock.json').read_text())['algorithm_reference']

def source(name):
    data = (args.source / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != lock['source_sha256'][name]:
        raise SystemExit('Locked upstream source digest mismatch: ' + name)
    return data.decode()

stars = source('src/data/stars.rs')
chinese = source('src/i18n/zh_cn.rs')
keys = dict(re.findall(r'StarKey::(\w+) => "([^"]+)"', stars))
names = {keys[k]: v for k, v in re.findall(r'StarKey::(\w+) => "([^"]+)"', chinese)}
brightness = {}
for key, body in re.findall(r'StarKey::(\w+) => Some\(\[(.*?)\]\)', stars, re.S):
    body = re.sub(r'//[^\n]*', '', body)
    row = [v.strip() for v in body.split(',') if v.strip()]
    assert len(row) == 12
    brightness[keys[key]] = [re.fullmatch(r's\((\w+)\)', v)[1].lower() if v != 'n' else '' for v in row]
stems = source('src/data/heavenly_stems.rs')
mutagens = [[keys[k] for k in re.findall(r'StarKey::(\w+)', row)]
            for row in re.findall(r'mutagen: \[(.*?)\]', stems, re.S)]
mutagens = [row for row in mutagens if row]
branches = source('src/data/earthly_branches.rs')
souls = [keys[k] for k in re.findall(r'soul: StarKey::(\w+)', branches)]
bodies = [keys[k] for k in re.findall(r'body: StarKey::(\w+)', branches)]
assert len(names) == len(keys) == 162
assert len(brightness) == 20 and len(mutagens) == 10
assert all(len(row) == 4 for row in mutagens) and len(souls) == len(bodies) == 12
quote = lambda s: json.dumps(s, ensure_ascii=False)
java_list = lambda values: 'List.of(' + ', '.join(map(quote, values)) + ')'
lines = ['package com.financeclaw.ziwei;', '', 'import java.util.List;', 'import java.util.Map;', '',
         '/** Generated constant tables from x-iztro 0.4.0; see import-ziwei-tables.py and MIT notices. */',
         'final class ZiweiTables {', '  private ZiweiTables() {}',
         '  static final Map<String, String> NAMES = Map.ofEntries(']
lines += ['    Map.entry(' + quote(k) + ', ' + quote(v) + ')' + (',' if i < len(names)-1 else ');')
          for i, (k, v) in enumerate(names.items())]
lines += ['  static final Map<String, List<String>> BRIGHTNESS = Map.ofEntries(']
lines += ['    Map.entry(' + quote(k) + ', ' + java_list(v) + ')' + (',' if i < len(brightness)-1 else ');')
          for i, (k, v) in enumerate(brightness.items())]
lines += ['  static final List<List<String>> MUTAGENS = List.of(' + ', '.join(java_list(row) for row in mutagens) + ');',
          '  static final List<String> SOULS = ' + java_list(souls) + ';',
          '  static final List<String> BODIES = ' + java_list(bodies) + ';', '}']
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text('\n'.join(lines) + '\n')
print('Generated constant tables:', args.output)
