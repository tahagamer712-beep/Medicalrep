#!/usr/bin/env python3
"""Restore an office database (or the central identity DB) from an encrypted
backup.

Requires --confirm: live data is never overwritten silently.
Restore identity FIRST, then offices — all from backups taken together.

Usage:
    MEDREP_DATA_DIR=./data python scripts/restore.py --office-id 1 \
        --file data/backups/office_1_20261011T000000.enc --confirm
    MEDREP_DATA_DIR=./data python scripts/restore.py --identity \
        --file data/backups/identity_20261011T000000.enc --confirm
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.databases import manager as dbm

parser = argparse.ArgumentParser()
g = parser.add_mutually_exclusive_group(required=True)
g.add_argument("--office-id", type=int)
g.add_argument("--identity", action="store_true")
g.add_argument("--manifest", type=str,
               help="restore a full backup set from a manifest file")
parser.add_argument("--file", type=str, required=False)
parser.add_argument("--confirm", action="store_true")
args = parser.parse_args()

data_dir = Path(os.environ.get("MEDREP_DATA_DIR", "./data"))

if args.manifest:
    result = dbm.restore_backup_set(data_dir, Path(args.manifest),
                                     confirm=args.confirm)
    print(f"restored set: {result['restored']}")
elif args.identity:
    dst = dbm.restore_identity(data_dir, Path(args.file),
                               confirm=args.confirm)
    print(f"restored identity from {args.file} -> {dst}")
else:
    dst = dbm.restore_office(data_dir, args.office_id, Path(args.file),
                             confirm=args.confirm)
    print(f"restored office {args.office_id} from {args.file} -> {dst}")
