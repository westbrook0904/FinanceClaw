#!/usr/bin/env python3
"""Print the server-managed profile path without reading the file or credentials."""
import argparse
import hashlib
from pathlib import Path
import struct

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--tenant', required=True)
parser.add_argument('--subject', required=True)
parser.add_argument('--epoch', type=int, required=True)
args = parser.parse_args()
if args.epoch < 1 or not args.tenant or not args.subject:
    parser.error('tenant, subject and a positive current privacy epoch are required')
digest = hashlib.sha256()
for part in (args.tenant, args.subject):
    encoded = part.encode('utf-8')
    digest.update(struct.pack('>I', len(encoded)))
    digest.update(encoded)
print(args.root / digest.hexdigest() / f'epoch-{args.epoch}' / 'profile.md')
