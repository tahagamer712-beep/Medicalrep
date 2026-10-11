#!/usr/bin/env python3
"""Idempotent demo seeding: 2 companies x 1 office each, with fake users.

Fake data only — names/phones are clearly synthetic (07xxxxxxxxx).
Safe to re-run: existing records are detected and skipped, never duplicated.

Usage:
    MEDREP_DATA_DIR=./data python scripts/init_demo.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.databases import manager as dbm
from app.identity import models as im
from app.identity import services as ids
from app.services import representatives as reps

def _data_dir() -> Path:
    # Read at call time (not import time) so tests can point at temp dirs.
    return Path(os.environ.get("MEDREP_DATA_DIR", "./data"))

# (name_ar, name_en, city, timezone)
COMPANIES = [
    ("شركة الشفاء للأدوية", "alshifa", "مكتب بغداد الرئيسي", "baghdad-hq", "بغداد", "الكرادة"),
    ("شركة الرازي العلمية", "alrazi", "مكتب البصرة", "basra-office", "البصرة", "المعقل"),
]

# (full_name, phone, role, company_idx, office_idx|None)
USERS = [
    ("المالك التجريبي", "07000000001", "owner", None, None, 100001),
    ("مدير الشفاء", "07000000002", "manager", 0, None, 100002),
    ("سوبرفايزر بغداد", "07000000003", "supervisor", 0, 0, 100003),
    ("مدير الرازي", "07000000004", "manager", 1, None, 100004),
    ("سوبرفايزر البصرة", "07000000005", "supervisor", 1, 1, 100005),
]

REPS = [
    # (full_name, phone, employee_code, company_idx, office_idx, supervisor_idx)
    ("مندوب أحمد", "07000000011", "SHF-001", 0, 0, 2),
    ("مندوب سارة", "07000000012", "SHF-002", 0, 0, 2),
    ("مندوب علي", "07000000013", "RZI-001", 1, 1, 4),
]


def main() -> None:
    DATA_DIR = _data_dir()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # ensure identity schema exists (alembic or create_all fallback)
    from app.identity.models import IdentityBase
    engine = dbm._engine_for(dbm.identity_db_path(DATA_DIR))
    IdentityBase.metadata.create_all(engine)

    with dbm.identity_session(DATA_DIR) as s:
        # bootstrap owner directly (no actor exists yet)
        owner = s.query(im.User).filter_by(phone="07000000001").first()
        if owner is None:
            owner = im.User(full_name="المالك التجريبي", phone="07000000001",
                            telegram_id=100001)
            s.add(owner)
            s.flush()
            s.add(im.Membership(user_id=owner.id, company_id=None, office_id=None,
                                role="owner"))
            print("created owner")
        owner_id = owner.id

    companies, offices = [], []
    with dbm.identity_session(DATA_DIR) as s:
        for name_ar, name_en, oname_ar, oname_en, city, area in COMPANIES:
            c = s.query(im.Company).filter_by(name_en=name_en).first()
            if c is None:
                c = ids.create_company(s, owner_id, name_ar=name_ar, name_en=name_en)
                print(f"created company {name_en}")
            companies.append(c.id)
            o = s.query(im.Office).filter_by(company_id=c.id, name_en=oname_en).first()
            if o is None:
                o = ids.create_office(s, DATA_DIR, owner_id, company_id=c.id,
                                      name_ar=oname_ar, name_en=oname_en,
                                      city=city, area=area)
                print(f"created office {oname_en} -> {o.db_path}")
            offices.append(o.id)

    with dbm.identity_session(DATA_DIR) as s:
        user_ids = {}
        for full_name, phone, role, ci, oi, tg in USERS:
            u = s.query(im.User).filter_by(phone=phone).first()
            if u is None:
                u = ids.create_user(s, owner_id, full_name=full_name, phone=phone,
                                    telegram_id=tg,
                                    company_id=companies[ci] if ci is not None else None)
                print(f"created user {full_name}")
            user_ids[phone] = u.id
            exists = (s.query(im.Membership)
                      .filter_by(user_id=u.id, role=role, status="active").first())
            if exists is None:
                ids.assign_membership(
                    s, owner_id, user_id=u.id,
                    company_id=companies[ci] if ci is not None else None,
                    office_id=offices[oi] if oi is not None else None,
                    role=role)
                print(f"assigned {role} -> {full_name}")

    for full_name, phone, code, ci, oi, sup_idx in REPS:
        with dbm.identity_session(DATA_DIR) as s:
            already = s.query(im.User).filter_by(phone=phone).first()
        if already is None:
            sup_phone = USERS[sup_idx][1]
            with dbm.identity_session(DATA_DIR) as s:
                sup = s.query(im.User).filter_by(phone=sup_phone).first()
            reps.add_representative(
                DATA_DIR, owner_id, office_id=offices[oi],
                full_name=full_name, phone=phone, employee_code=code,
                supervisor_user_id=sup.id, hire_date="2026-01-15")
            print(f"created rep {full_name} ({code})")
        else:
            print(f"rep {full_name} already exists — skipped")

    print("demo seeding complete (idempotent).")


if __name__ == "__main__":
    main()
