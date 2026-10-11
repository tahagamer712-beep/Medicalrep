"""Payroll: periods, salary records, and the append-only financial ledger.

Agreed rules (enforced here, tested in test_payroll.py):
- Direct deduction by an authorized supervisor/manager applies IMMEDIATELY
  after confirmation; the rep is notified and everything is logged.
- An auto/bot-proposed deduction stays PENDING and never affects the salary
  until a supervisor approves it.
- Every movement carries a unique uuid: a retried call with the same uuid
  returns the existing row — never a second movement.
- The ledger is append-only: corrections are NEW reversal rows; the original
  row is never edited or silently deleted.
- Money is integer IQD (no float rounding issues).

Notification: reps get an immediate NotificationJob on direct deductions and
on proposal decisions (sent through the normal DB-backed job runner).
"""
from __future__ import annotations

import json
import uuid as uuid_lib
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event

_KINDS = ("deduction_direct", "deduction_proposed", "bonus", "correction")


def _require_active_rep(isess: Session, rep_user_id: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=rep_user_id, office_id=office_id, role="rep",
                    status="active").first())
    if m is None:
        raise PermissionDenied("representative has no active membership here")


def _get_open_period(osess: Session, period_id: int) -> om.PayrollPeriod:
    p = osess.get(om.PayrollPeriod, period_id)
    if p is None:
        raise NotFound("payroll period not found")
    if p.status != "open":
        raise ValidationError("payroll period is finalized; no new movements")
    return p


def _notify(osess: Session, office_id: int, user_id: int, text: str,
            kind: str) -> None:
    osess.add(om.NotificationJob(
        job_key=f"{kind}:{office_id}:{user_id}:{uuid_lib.uuid4().hex[:12]}",
        kind=kind, scheduled_for=utcnow(),
        payload_json=json.dumps({"user_id": user_id, "text": text},
                                ensure_ascii=False)))


def _find_by_uuid(osess: Session, txn_uuid: str) -> om.FinancialTransaction | None:
    return osess.query(om.FinancialTransaction).filter_by(uuid=txn_uuid).first()


# --- periods ---------------------------------------------------------------

def open_payroll_period(data_dir: Path, actor: int, *, office_id: int,
                        year: int, month: int, name: str = "") -> dict:
    if not 1 <= month <= 12:
        raise ValidationError("month must be 1-12")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "payroll.manage", office_id=office_id)
        rep_ids = [m.user_id for m in s.query(im.Membership)
                   .filter_by(office_id=office_id, role="rep", status="active").all()]
    with dbm.office_session(data_dir, office_id) as osess:
        if osess.query(om.PayrollPeriod).filter_by(year=year, month=month).first():
            raise AlreadyExists("payroll period already exists")
        p = om.PayrollPeriod(name=name or f"{year}-{month:02d}", year=year,
                             month=month,
                             start_date=f"{year}-{month:02d}-01",
                             end_date=f"{year}-{month:02d}-28",
                             created_by=actor)
        osess.add(p)
        osess.flush()
        for rid in rep_ids:
            osess.add(om.SalaryRecord(
                payroll_period_id=p.id, representative_id=rid,
                base_salary=_base_for_period(osess, rid, p.start_date)))
        log_office_event(osess, actor_user_id=actor,
                         event_type="PAYROLL_PERIOD_OPENED",
                         entity="payroll_period", entity_id=p.id)
        return {"period_id": p.id, "reps": len(rep_ids)}


def set_base_salary(data_dir: Path, actor: int, *, office_id: int,
                    period_id: int, rep_user_id: int, amount: int) -> dict:
    """Backward-compatible: sets base for one open period AND logs history
    effective from that period's start."""
    with dbm.office_session(data_dir, office_id) as osess:
        p = osess.get(om.PayrollPeriod, period_id)
        if p is None:
            raise NotFound("payroll period not found")
        start = p.start_date
    return change_base_salary(data_dir, actor, office_id=office_id,
                              rep_user_id=rep_user_id, amount=amount,
                              effective_from=start, reason="manual set")


def change_base_salary(data_dir: Path, actor: int, *, office_id: int,
                       rep_user_id: int, amount: int, effective_from: str,
                       reason: str = "") -> dict:
    """Raise/change base salary with an effective date. History is append-only;
    the change applies to open periods starting on/after effective_from."""
    if amount < 0:
        raise ValidationError("base salary cannot be negative")
    try:
        from datetime import date as _d
        _d.fromisoformat(effective_from)
    except ValueError:
        raise ValidationError("effective_from must be YYYY-MM-DD")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "payroll.manage", office_id=office_id)
        _require_active_rep(s, rep_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        osess.add(om.BaseSalaryChange(
            representative_id=rep_user_id, amount=int(amount),
            effective_from=effective_from, reason=reason.strip(),
            changed_by=actor))
        affected = 0
        for p in osess.query(om.PayrollPeriod).filter_by(status="open").all():
            if p.start_date >= effective_from:
                rec = (osess.query(om.SalaryRecord)
                       .filter_by(payroll_period_id=p.id,
                                  representative_id=rep_user_id).first())
                if rec:
                    rec.base_salary = int(amount)
                    rec.updated_at = utcnow()
                    affected += 1
        log_office_event(osess, actor_user_id=actor,
                         event_type="BASE_SALARY_CHANGED",
                         entity="representative", entity_id=rep_user_id,
                         reason=reason,
                         meta=json.dumps({"amount": amount,
                                          "effective_from": effective_from}))
        return {"rep": rep_user_id, "amount": int(amount),
                "effective_from": effective_from,
                "periods_affected": affected}


def list_salary_history(data_dir: Path, actor: int, *, office_id: int,
                        rep_user_id: int) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        try:
            require_permission(s, actor, "payroll.manage", office_id=office_id)
        except PermissionDenied:
            m = (s.query(im.Membership)
                 .filter_by(user_id=actor, office_id=office_id, role="rep",
                            status="active").first())
            if m is None or actor != rep_user_id:
                raise PermissionDenied("can only view own salary history")
    with dbm.office_session(data_dir, office_id) as osess:
        rows = (osess.query(om.BaseSalaryChange)
                .filter_by(representative_id=rep_user_id)
                .order_by(om.BaseSalaryChange.effective_from).all())
        return [{"amount": r.amount, "effective_from": r.effective_from,
                 "reason": r.reason, "changed_by": r.changed_by,
                 "at": r.created_at.isoformat()} for r in rows]


def _base_for_period(osess: Session, rep_user_id: int,
                     period_start: str) -> int:
    row = (osess.query(om.BaseSalaryChange)
           .filter(om.BaseSalaryChange.representative_id == rep_user_id,
                   om.BaseSalaryChange.effective_from <= period_start)
           .order_by(om.BaseSalaryChange.effective_from.desc()).first())
    return row.amount if row else 0


def finalize_period(data_dir: Path, actor: int, *, office_id: int,
                    period_id: int) -> dict:
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "payroll.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        p = osess.get(om.PayrollPeriod, period_id)
        if p is None:
            raise NotFound("payroll period not found")
        if p.status == "finalized":
            raise ValidationError("period already finalized")
        for rec in osess.query(om.SalaryRecord).filter_by(
                payroll_period_id=p.id).all():
            totals = _totals(osess, p.id, rec.representative_id)
            rec.total_bonuses = totals["bonuses"]
            rec.total_deductions = totals["deductions"]
            rec.net_salary = rec.base_salary + totals["bonuses"] - totals["deductions"]
            rec.status = "finalized"
            rec.finalized_at = utcnow()
            rec.updated_at = utcnow()
        p.status = "finalized"
        log_office_event(osess, actor_user_id=actor,
                         event_type="PAYROLL_PERIOD_FINALIZED",
                         entity="payroll_period", entity_id=p.id)
        return {"period_id": p.id, "status": p.status}


# --- movements ---------------------------------------------------------------

def _create_txn(osess: Session, *, rep_user_id: int, period_id: int | None,
                kind: str, signed_amount: int, status: str, reason: str,
                actor: int, txn_uuid: str,
                related_uuid: str | None = None
                ) -> tuple[om.FinancialTransaction, bool]:
    """Atomic get-or-create by uuid. Returns (row, created).

    Uses INSERT ... ON CONFLICT DO NOTHING so concurrent callers with the
    same uuid can never create two rows: exactly one wins, the losers get
    the winner's row with created=False.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = (sqlite_insert(om.FinancialTransaction)
            .values(uuid=txn_uuid, representative_id=rep_user_id,
                    payroll_period_id=period_id, kind=kind,
                    signed_amount=int(signed_amount), status=status,
                    reason=reason, related_uuid=related_uuid,
                    created_by=actor,
                    applied_at=utcnow() if status == "applied" else None)
            .on_conflict_do_nothing(index_elements=["uuid"]))
    result = osess.execute(stmt)
    row = _find_by_uuid(osess, txn_uuid)
    return row, result.rowcount == 1


def apply_direct_deduction(data_dir: Path, actor: int, *, office_id: int,
                           rep_user_id: int, period_id: int, amount: int,
                           reason: str,
                           idempotency_key: str | None = None) -> dict:
    """Authorized deduction: applies IMMEDIATELY + notifies the rep."""
    if amount <= 0:
        raise ValidationError("amount must be positive")
    if not reason.strip():
        raise ValidationError("deduction reason is required")
    key = idempotency_key or uuid_lib.uuid4().hex
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "deduction.direct", office_id=office_id)
        _require_active_rep(s, rep_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        p = _get_open_period(osess, period_id)
        t, created = _create_txn(osess, rep_user_id=rep_user_id, period_id=p.id,
                                 kind="deduction_direct",
                                 signed_amount=-int(amount),
                                 status="applied", reason=reason.strip(),
                                 actor=actor, txn_uuid=key)
        if created:
            log_office_event(osess, actor_user_id=actor,
                             event_type="DEDUCTION_APPLIED",
                             entity="financial_transaction", entity_id=t.id,
                             reason=reason,
                             meta=json.dumps({"amount": amount, "uuid": t.uuid}))
            _notify(osess, office_id, rep_user_id,
                    f"💸 تم تطبيق خصم {amount:,} د.ع على راتبك. "
                    f"السبب: {reason.strip()}",
                    "deduction_notice")
        return {"uuid": t.uuid, "status": t.status,
                "net_effect": t.signed_amount, "created": created}


def propose_deduction(data_dir: Path, actor: int, *, office_id: int,
                      rep_user_id: int, period_id: int, amount: int,
                      reason: str,
                      idempotency_key: str | None = None) -> dict:
    """Bot/auto proposal: PENDING — never affects salary until approved."""
    if amount <= 0:
        raise ValidationError("amount must be positive")
    key = idempotency_key or uuid_lib.uuid4().hex
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "deduction.propose", office_id=office_id)
        _require_active_rep(s, rep_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        p = _get_open_period(osess, period_id)
        t, created = _create_txn(osess, rep_user_id=rep_user_id, period_id=p.id,
                                 kind="deduction_proposed",
                                 signed_amount=-int(amount),
                                 status="pending", reason=reason.strip(),
                                 actor=actor, txn_uuid=key)
        if created:
            log_office_event(osess, actor_user_id=actor,
                             event_type="DEDUCTION_PROPOSED",
                             entity="financial_transaction", entity_id=t.id,
                             reason=reason)
        return {"uuid": t.uuid, "status": t.status, "created": created}


def review_proposed_deduction(data_dir: Path, actor: int, *, office_id: int,
                              txn_uuid: str, decision: str,
                              notes: str = "") -> dict:
    if decision not in ("approve", "reject"):
        raise ValidationError("decision must be approve or reject")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "deduction.review", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        t = _find_by_uuid(osess, txn_uuid)
        if t is None or t.kind != "deduction_proposed":
            raise NotFound("proposed deduction not found")
        target = "applied" if decision == "approve" else "rejected"
        if target not in om.TXN_TRANSITIONS.get(t.status, set()):
            raise ValidationError(f"proposal is already {t.status}")
        t.status = target
        t.approved_by = actor
        t.approved_at = utcnow()
        if target == "applied":
            t.applied_at = utcnow()
        log_office_event(osess, actor_user_id=actor,
                         event_type="DEDUCTION_" + target.upper(),
                         entity="financial_transaction", entity_id=t.id,
                         reason=notes)
        word = "اعتُمد" if target == "applied" else "رُفض"
        _notify(osess, office_id, t.representative_id,
                f"📋 الخصم المقترح {word}: {abs(t.signed_amount):,} د.ع. {notes}",
                "deduction_notice")
        return {"uuid": t.uuid, "status": t.status}


def grant_bonus(data_dir: Path, actor: int, *, office_id: int,
                rep_user_id: int, period_id: int, amount: int, reason: str,
                idempotency_key: str | None = None) -> dict:
    if amount <= 0:
        raise ValidationError("amount must be positive")
    key = idempotency_key or uuid_lib.uuid4().hex
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "bonus.grant", office_id=office_id)
        _require_active_rep(s, rep_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        p = _get_open_period(osess, period_id)
        t, created = _create_txn(osess, rep_user_id=rep_user_id, period_id=p.id,
                                 kind="bonus", signed_amount=int(amount),
                                 status="applied", reason=reason.strip(),
                                 actor=actor, txn_uuid=key)
        if created:
            log_office_event(osess, actor_user_id=actor,
                             event_type="BONUS_GRANTED",
                             entity="financial_transaction", entity_id=t.id,
                             reason=reason)
        return {"uuid": t.uuid, "status": t.status, "created": created}


def reverse_transaction(data_dir: Path, actor: int, *, office_id: int,
                        txn_uuid: str, reason: str,
                        idempotency_key: str | None = None) -> dict:
    """Correction = NEW reversal row. The original is never touched."""
    if not reason.strip():
        raise ValidationError("reversal reason is required")
    key = idempotency_key or uuid_lib.uuid4().hex
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "transaction.reverse", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        orig = _find_by_uuid(osess, txn_uuid)
        if orig is None:
            raise NotFound("transaction not found")
        if orig.status != "applied":
            raise ValidationError("only applied transactions can be reversed")
        if orig.kind == "correction":
            raise ValidationError("cannot reverse a correction")
        if orig.payroll_period_id:
            _get_open_period(osess, orig.payroll_period_id)
        rev, created = _create_txn(
            osess, rep_user_id=orig.representative_id,
            period_id=orig.payroll_period_id, kind="correction",
            signed_amount=-orig.signed_amount, status="applied",
            reason=reason.strip(), actor=actor, txn_uuid=key,
            related_uuid=orig.uuid)
        if created:
            log_office_event(osess, actor_user_id=actor,
                             event_type="TRANSACTION_REVERSED",
                             entity="financial_transaction", entity_id=rev.id,
                             reason=reason,
                             meta=json.dumps({"reverses": orig.uuid}))
        return {"uuid": rev.uuid, "reverses": orig.uuid,
                "net_effect": rev.signed_amount, "created": created}


# --- reads -------------------------------------------------------------------

def _totals(osess: Session, period_id: int, rep_user_id: int) -> dict:
    txns = (osess.query(om.FinancialTransaction)
            .filter_by(payroll_period_id=period_id,
                       representative_id=rep_user_id, status="applied").all())
    by_uuid = {t.uuid: t for t in txns}
    bonuses = deductions = 0
    for t in txns:
        amt, kind = t.signed_amount, t.kind
        if kind == "correction" and t.related_uuid in by_uuid:
            # a reversal nets against its original's category
            if by_uuid[t.related_uuid].kind == "bonus":
                bonuses += amt  # amt is negative: reduces bonuses
            else:
                deductions -= amt  # amt is positive: reduces deductions
        elif amt > 0:
            bonuses += amt
        else:
            deductions -= amt
    return {"bonuses": bonuses, "deductions": deductions}


def salary_summary(data_dir: Path, actor: int, *, office_id: int,
                   rep_user_id: int, period_id: int) -> dict:
    with dbm.identity_session(data_dir) as s:
        try:
            require_permission(s, actor, "deduction.review", office_id=office_id)
        except PermissionDenied:
            # reps may view only their own summary
            m = (s.query(im.Membership)
                 .filter_by(user_id=actor, office_id=office_id, role="rep",
                            status="active").first())
            if m is None or actor != rep_user_id:
                raise PermissionDenied("can only view own salary")
    with dbm.office_session(data_dir, office_id) as osess:
        p = osess.get(om.PayrollPeriod, period_id)
        if p is None:
            raise NotFound("payroll period not found")
        rec = (osess.query(om.SalaryRecord)
               .filter_by(payroll_period_id=p.id, representative_id=rep_user_id)
               .first())
        if rec is None:
            raise NotFound("salary record not found")
        totals = _totals(osess, p.id, rep_user_id)
        return {"period": p.name, "status": p.status,
                "base_salary": rec.base_salary,
                "bonuses": totals["bonuses"],
                "deductions": totals["deductions"],
                "net": rec.base_salary + totals["bonuses"] - totals["deductions"]}


def list_transactions(data_dir: Path, actor: int, *, office_id: int,
                      rep_user_id: int | None = None,
                      status: str | None = None) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        try:
            require_permission(s, actor, "deduction.review", office_id=office_id)
            scope_rep = rep_user_id
        except PermissionDenied:
            m = (s.query(im.Membership)
                 .filter_by(user_id=actor, office_id=office_id, role="rep",
                            status="active").first())
            if m is None:
                raise PermissionDenied("no access to financial records")
            scope_rep = actor
    with dbm.office_session(data_dir, office_id) as osess:
        q = (osess.query(om.FinancialTransaction)
             .order_by(om.FinancialTransaction.id.desc()))
        if scope_rep is not None:
            q = q.filter_by(representative_id=scope_rep)
        if status:
            q = q.filter_by(status=status)
        return [{"uuid": t.uuid, "rep": t.representative_id, "kind": t.kind,
                 "amount": t.signed_amount, "status": t.status,
                 "reason": t.reason} for t in q.limit(100).all()]
