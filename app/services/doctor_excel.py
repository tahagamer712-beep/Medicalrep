"""Doctor Excel import/export.

- Export: rep gets assigned doctors only; supervisor+ gets the whole office.
- Every exported row carries Doctor Code (stable id) + Version (conflict
  detection). These two columns are the ONLY trusted identifiers.
- Import NEVER trusts company/office/user ids from the file: scope and
  permissions are enforced server-side.
- Flow: preview (validate + counts + row errors + duplicates + conflicts)
  -> user fixes file -> apply with confirm=True.
- Rules: never delete doctors missing from the file; empty/bad files are
  rejected without touching the DB; the same file (sha256) cannot be applied
  twice; every import is logged; imports are rollback-able (documented below).

Rollback semantics: rollback deactivates doctors the import CREATED and
restores field values the import CHANGED (via doctor_history entries tagged
with the import id). It never deletes rows; the rollback itself is logged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services import doctors as docs
from app.services.audit import log_office_event

# Fixed column order. Doctor Code + Version are system columns (read-only).
COLUMNS = [
    "Doctor Code", "Version",
    "Dr Name", "Area", "Sub Area", "Nearest Pharmacy", "Speciality",
    "Class", "TGT Item", "Additional Informations",
    "Reception Days", "Non Reception Days", "Reception Time",
    "Doctor Notes", "Dealer Info", "Status",
]

INSTRUCTIONS = [
    ["تعليمات ملف الأطباء"],
    ["- عمودا Doctor Code و Version للقراءة فقط؛ لا تعدّلهما."],
    ["- TGT Item: أسماء المنتجات التجارية مفصولة بـ ; مثال: Panadol; Brufen"],
    ["- Reception Days: أرقام أيام الأسبوع مفصولة بـ , (0=الاثنين .. 6=الأحد)"],
    ["- Class: A أو B أو Z أو A-B أو فارغ"],
    ["- Status: Active أو Inactive أو Left"],
    ["- Nearest Pharmacy: اسم صيدلية موجودة في النظام"],
    ["- الصفوف الفارغة تُتجاهل. الأطباء الغائبون عن الملف لا يُحذفون."],
]


def _scope_all(isess, actor: int, office_id: int) -> bool:
    """True if actor may export/import the whole office (supervisor+)."""
    try:
        require_permission(isess, actor, "directory.manage", office_id=office_id)
        return True
    except PermissionDenied:
        m = (isess.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, role="rep",
                        status="active").first())
        if m is None:
            raise PermissionDenied("no access to doctors here")
        return False


def _doctor_row(osess, d: om.Doctor) -> list:
    tgt_names = []
    for dp in osess.query(om.DoctorProduct).filter_by(doctor_id=d.id).all():
        p = osess.get(om.Product, dp.product_id)
        if p:
            tgt_names.append(p.trade_name)
    pharm = osess.get(om.Pharmacy, d.nearest_pharmacy_id) \
        if d.nearest_pharmacy_id else None
    return [
        d.external_code, d.version, d.name, d.area, d.sub_area,
        pharm.name if pharm else "", d.specialty, d.doctor_class,
        "; ".join(tgt_names), d.additional_info,
        ",".join(map(str, json.loads(d.reception_days_json or "[]"))),
        ",".join(map(str, json.loads(d.non_reception_days_json or "[]"))),
        d.reception_time, d.doctor_notes, d.dealer_info, d.status,
    ]


def export_doctors(data_dir: Path, actor: int, *, office_id: int,
                   out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with dbm.identity_session(data_dir) as s:
        whole = _scope_all(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.Doctor).order_by(om.Doctor.name)
        if not whole:
            q = q.filter(om.Doctor.assigned_rep_id == actor)
        doctors = q.all()
        wb = Workbook()
        ws = wb.active
        ws.title = "Doctors"
        ws.append(COLUMNS)
        for d in doctors:
            ws.append(_doctor_row(osess, d))
        ws2 = wb.create_sheet("Instructions")
        for row in INSTRUCTIONS:
            ws2.append(row)
        wb.save(out_path)
    return out_path


def export_template(out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Doctors"
    ws.append(COLUMNS)
    ws.append(["", "", "مثال: د. أحمد", "المنصور", "شارع الأميرات", "",
               "باطنية", "A", "Panadol", "", "0,2", "", "09:00-12:00",
               "", "", "Active"])
    ws2 = wb.create_sheet("Instructions")
    for row in INSTRUCTIONS:
        ws2.append(row)
    wb.save(out_path)
    return out_path


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_rows(path: Path) -> list[dict]:
    wb = load_workbook(path, data_only=True)
    if "Doctors" not in wb.sheetnames:
        raise ValidationError("missing 'Doctors' sheet")
    ws = wb["Doctors"]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValidationError("file is empty")
    header = [(c or "") for c in rows[0]]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise ValidationError(f"missing columns: {', '.join(missing)}")
    idx = {c: header.index(c) for c in COLUMNS}
    out = []
    for lineno, r in enumerate(rows[1:], start=2):
        vals = {c: (r[idx[c]] if idx[c] < len(r) else None) for c in COLUMNS}
        if all(v in (None, "") for v in vals.values()):
            continue  # blank row
        vals["_lineno"] = lineno
        out.append(vals)
    if not out:
        raise ValidationError("file has no data rows")
    return out


def _parse_row(osess, vals: dict) -> tuple[dict, list[str]]:
    """Returns (parsed_fields, errors)."""
    errors = []
    f: dict = {}
    name = str(vals["Dr Name"] or "").strip()
    if not name:
        errors.append("Dr Name مطلوب")
    f["name"] = name
    for key, col in (("area", "Area"), ("sub_area", "Sub Area"),
                     ("specialty", "Speciality"),
                     ("additional_info", "Additional Informations"),
                     ("reception_time", "Reception Time"),
                     ("doctor_notes", "Doctor Notes"),
                     ("dealer_info", "Dealer Info")):
        f[key] = str(vals[col] or "").strip()
    cls = str(vals["Class"] or "").strip()
    if cls not in docs.CLASSES:
        errors.append(f"Class غير صالح: {cls}")
    f["doctor_class"] = cls
    status = str(vals["Status"] or "").strip().lower()
    mapping = {"active": "active", "inactive": "inactive", "left": "left"}
    if status not in mapping:
        errors.append(f"Status غير صالح: {vals['Status']}")
    f["status"] = mapping.get(status, "active")
    for key, col in (("reception_days_json", "Reception Days"),
                     ("non_reception_days_json", "Non Reception Days")):
        raw = str(vals[col] or "").strip()
        try:
            days = [int(x) for x in raw.split(",") if x.strip()] if raw else []
            if any(d not in docs.WEEKDAYS for d in days):
                raise ValueError
            f[key] = json.dumps(sorted(set(days)))
        except ValueError:
            errors.append(f"{col}: أيام 0-6 مفصولة بـ ,")
    pharm_name = str(vals["Nearest Pharmacy"] or "").strip()
    if pharm_name:
        ph = osess.query(om.Pharmacy).filter_by(name=pharm_name).first()
        if ph is None or ph.status != "active":
            errors.append(f"الصيدلية غير موجودة: {pharm_name}")
        else:
            f["nearest_pharmacy_id"] = ph.id
    else:
        f["nearest_pharmacy_id"] = None
    tgt_raw = str(vals["TGT Item"] or "").strip()
    tgt_ids = []
    for tname in [t.strip() for t in tgt_raw.split(";") if t.strip()]:
        p = osess.query(om.Product).filter_by(trade_name=tname).first()
        if p is None or p.status != "active":
            errors.append(f"المنتج غير موجود: {tname}")
        else:
            tgt_ids.append(p.id)
    f["_tgt_ids"] = tgt_ids
    return f, errors


def preview_import(data_dir: Path, actor: int, *, office_id: int,
                   file_path: str | Path) -> dict:
    """Validate without writing. Returns counts + row errors + duplicates
    + conflicts. The file is NOT applied."""
    path = Path(file_path)
    file_hash = _file_hash(path)
    with dbm.identity_session(data_dir) as s:
        whole = _scope_all(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        if osess.query(om.ImportLog).filter_by(file_hash=file_hash).first():
            raise AlreadyExists("this file was already imported")
        rows = _read_rows(path)
        new, updated, errors, duplicates, conflicts = 0, 0, [], [], []
        seen_codes = set()
        for vals in rows:
            lineno = vals["_lineno"]
            code = str(vals["Doctor Code"] or "").strip()
            if code in seen_codes and code:
                errors.append({"row": lineno,
                               "reason": "كود مكرر داخل الملف"})
                continue
            seen_codes.add(code)
            fields, errs = _parse_row(osess, vals)
            if errs:
                errors.append({"row": lineno, "reason": "; ".join(errs)})
                continue
            if code:
                d = osess.query(om.Doctor).filter_by(
                    external_code=code).first()
                if d is None:
                    errors.append({"row": lineno,
                                   "reason": "كود الطبيب غير معروف"})
                    continue
                if not whole and d.assigned_rep_id != actor:
                    errors.append({"row": lineno,
                                   "reason": "خارج نطاق صلاحياتك"})
                    continue
                try:
                    ver = int(vals["Version"] or 0)
                except (ValueError, TypeError):
                    ver = 0
                if ver != d.version:
                    conflicts.append({"row": lineno, "code": code,
                                      "name": d.name,
                                      "reason": f"تعديل متزامن (نسختك {ver}، "
                                                f"الحالية {d.version})"})
                    continue
                updated += 1
            else:
                sims = docs.find_similar_doctors(osess, fields["name"])
                if sims:
                    duplicates.append({"row": lineno, "name": fields["name"],
                                       "similar": sims[:3]})
                new += 1
        return {"file_hash": file_hash, "rows": len(rows), "new": new,
                "updated": updated, "errors": errors, "duplicates": duplicates,
                "conflicts": conflicts,
                "can_apply": not errors and not conflicts}


def apply_import(data_dir: Path, actor: int, *, office_id: int,
                 file_path: str | Path, confirm: bool = False) -> dict:
    """Apply a previewed file. confirm=True REQUIRED. Refuses to apply when
    any row has errors or conflicts."""
    if not confirm:
        raise ValidationError("explicit confirmation required to apply import")
    path = Path(file_path)
    preview = preview_import(data_dir, actor, office_id=office_id,
                             file_path=path)
    if not preview["can_apply"]:
        raise ValidationError(
            f"cannot apply: {len(preview['errors'])} errors, "
            f"{len(preview['conflicts'])} conflicts — fix the file and re-upload")
    with dbm.identity_session(data_dir) as s:
        whole = _scope_all(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        log = om.ImportLog(kind="doctors", file_name=path.name,
                           file_hash=preview["file_hash"], uploaded_by=actor,
                           status="in_progress")
        osess.add(log)
        osess.flush()
        tag = f"excel_import:{log.id}"
        rows = _read_rows(path)
        added = updated = 0
        for vals in rows:
            fields, _ = _parse_row(osess, vals)
            code = str(vals["Doctor Code"] or "").strip()
            tgt_ids = fields.pop("_tgt_ids")
            if code:
                d = osess.query(om.Doctor).filter_by(
                    external_code=code).first()
                for f in docs.TRACKED_FIELDS:
                    if f in fields:
                        old, newv = getattr(d, f), fields[f]
                        if str(old) != str(newv):
                            docs._log_history(osess, d.id, actor, f,
                                              str(old), str(newv), tag)
                            setattr(d, f, newv)
                d.version += 1
                d.updated_at = utcnow()
                _sync_tgt(osess, d.id, tgt_ids, actor, tag)
                updated += 1
            else:
                import uuid as _uuid
                d = om.Doctor(external_code=_uuid.uuid4().hex[:16],
                              created_by=actor, version=1)
                for f in docs.TRACKED_FIELDS:
                    if f in fields:
                        setattr(d, f, fields[f])
                if not whole:
                    # reps may only create doctors under themselves
                    d.assigned_rep_id = actor
                osess.add(d)
                osess.flush()
                docs._log_history(osess, d.id, actor, "created", "", d.name,
                                  tag)
                _sync_tgt(osess, d.id, tgt_ids, actor, tag)
                added += 1
        log.counts_json = json.dumps({"added": added, "updated": updated,
                                      "duplicates_warned":
                                      len(preview["duplicates"])},
                                     ensure_ascii=False)
        log.status = "applied"
        log_office_event(osess, actor_user_id=actor,
                         event_type="DOCTORS_IMPORTED", entity="import_log",
                         entity_id=log.id)
        return {"import_id": log.id, "added": added, "updated": updated}


def _sync_tgt(osess, doctor_id: int, tgt_ids: list[int], actor: int,
              tag: str) -> None:
    current = {dp.product_id for dp in osess.query(om.DoctorProduct)
               .filter_by(doctor_id=doctor_id).all()}
    wanted = set(tgt_ids)
    for pid in wanted - current:
        osess.add(om.DoctorProduct(doctor_id=doctor_id, product_id=pid,
                                   added_by=actor))
    for pid in current - wanted:
        (osess.query(om.DoctorProduct)
         .filter_by(doctor_id=doctor_id, product_id=pid).delete())
    if current != wanted:
        docs._log_history(osess, doctor_id, actor, "tgt_products",
                          ",".join(map(str, sorted(current))),
                          ",".join(map(str, sorted(wanted))), tag)


def rollback_import(data_dir: Path, actor: int, *, office_id: int,
                    import_id: int) -> dict:
    """Reverse an import: created doctors -> inactive; changed fields ->
    restored. Rows are never deleted; the rollback is itself logged."""
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "directory.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        log = osess.get(om.ImportLog, import_id)
        if log is None:
            raise NotFound("import log not found")
        if log.status != "applied":
            raise ValidationError(f"import is {log.status}, cannot rollback")
        tag = f"excel_import:{log.id}"
        entries = (osess.query(om.DoctorHistory)
                   .filter(om.DoctorHistory.reason == tag)
                   .order_by(om.DoctorHistory.id.desc()).all())
        touched = set()
        for h in entries:
            d = osess.get(om.Doctor, h.doctor_id)
            if d is None:
                continue
            touched.add(d.id)
            if h.field_name == "created":
                if d.status == "active":
                    docs._log_history(osess, d.id, actor, "status", "active",
                                      "inactive", f"rollback:{log.id}")
                    d.status = "inactive"
            elif h.field_name == "tgt_products":
                continue  # TGT handled below via full resync is out of scope;
                # documented limitation
            else:
                docs._log_history(osess, d.id, actor, h.field_name,
                                  h.new_value, h.old_value,
                                  f"rollback:{log.id}")
                setattr(d, h.field_name,
                        _coerce(h.field_name, h.old_value))
            d.version += 1
            d.updated_at = utcnow()
        log.status = "rolled_back"
        log_office_event(osess, actor_user_id=actor,
                         event_type="DOCTORS_IMPORT_ROLLED_BACK",
                         entity="import_log", entity_id=log.id)
        return {"import_id": log.id, "doctors_touched": len(touched),
                "note": "tgt_products changes are not auto-reversed; "
                        "adjust manually if needed"}


def _coerce(field: str, value: str):
    if field in ("nearest_pharmacy_id", "assigned_rep_id"):
        return int(value) if value and value != "None" else None
    return value
