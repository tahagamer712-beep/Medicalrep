#!/usr/bin/env python3
"""Encrypted backup of an office database, or the central identity DB.

Usage:
    MEDREP_DATA_DIR=./data python scripts/backup.py --office-id 1
    MEDREP_DATA_DIR=./data python scripts/backup.py --identity
    MEDREP_DATA_DIR=./data python scripts/backup.py --all   # identity + offices

A full backup is identity + every office, taken back-to-back.
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
g.add_argument("--all", action="store_true", dest="all_")
args = parser.parse_args()

data_dir = Path(os.environ.get("MEDREP_DATA_DIR", "./data"))

if args.identity or args.all_:
    identity_out = dbm.backup_identity(data_dir)
    print(f"identity backup written: {identity_out} "
          f"({identity_out.stat().st_size} bytes, Fernet-encrypted)")

if args.office_id is not None or args.all_:
    office_ids = [args.office_id] if args.office_id is not None else None
    if office_ids is None:
        from app.identity import models as im
        with dbm.identity_session(data_dir) as s:
            office_ids = [o.id for o in s.query(im.Office).all()]
    office_files = {}
    for oid in office_ids:
        out = dbm.backup_office(data_dir, oid)
        print(f"backup written: {out} ({out.stat().st_size} bytes, "
              f"Fernet-encrypted)")
        office_files[oid] = out

if args.all_:
    manifest = dbm.write_backup_manifest(data_dir, identity_out, office_files)
    print(f"backup set manifest: {manifest}")
