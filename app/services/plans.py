"""Weekly/daily plans with explicit states and immutable version history.

State machine: draft -> submitted -> under_review -> approved | changes_requested
               changes_requested -> draft (rep revises)
               approved -> draft (rep starts a new version; old revision marked superseded)

- Draft edits never create revisions; every SUBMIT snapshots a PlanRevision.
- Revisions are append-only: never updated, never deleted.
- Terminated reps (no active membership) cannot create or touch plans.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event

_PLAN_MODELS = {"weekly": om.WeeklyPlan, "daily": om.DailyPlan}
_PERIOD = {"weekly": "week_start", "daily": "date"}


def _require_active_rep(isess: Session, rep_user_id: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=rep_user_id, office_id=office_id, role="rep",
                    status="active").first())
    if m is None:
        raise PermissionDenied("representative has no active membership here")


def _office_access(isess: Session, actor: int, office_id: int) -> bool:
    """Check office-level plan permission FIRST (before touching office data).
    Returns True if the actor is a reviewer (supervisor+)."""
    try:
        require_permission(isess, actor, "plan.review", office_id=office_id)
        return True
    except PermissionDenied:
        require_permission(isess, actor, "plan.own", office_id=office_id)
        return False


def _require_own_or_reviewer(is_reviewer: bool, actor: int, rep_user_id: int) -> None:
    if not is_reviewer and actor != rep_user_id:
        raise PermissionDenied("can only manage own plans")


def _get_plan(osess: Session, plan_type: str, plan_id: int):
    model = _PLAN_MODELS[plan_type]
    plan = osess.get(model, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    return plan


def _transition(plan, to: str) -> None:
    allowed = om.PLAN_TRANSITIONS.get(plan.status, set())
    if to not in allowed:
        raise ValidationError(f"cannot move plan from {plan.status} to {to}")
    plan.status = to
    plan.updated_at = utcnow()


def create_plan(data_dir: Path, actor: int, *, office_id: int, plan_type: str,
                rep_user_id: int, period: str, payload: dict) -> dict:
    """Create a draft plan. One plan per rep per period (unique)."""
    if plan_type not in _PLAN_MODELS:
        raise ValidationError("plan_type must be weekly or daily")
    with dbm.identity_session(data_dir) as s:
        _office_access(s, actor, office_id)
        _require_active_rep(s, rep_user_id, office_id)
        if actor != rep_user_id:
            # creating for someone else requires reviewer rights
            require_permission(s, actor, "plan.review", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        model = _PLAN_MODELS[plan_type]
        period_col = _PERIOD[plan_type]
        exists = osess.query(model).filter_by(
            representative_id=rep_user_id, **{period_col: period}).first()
        if exists:
            raise AlreadyExists("a plan already exists for this period")
        plan = model(representative_id=rep_user_id, **{period_col: period},
                     payload_json=json.dumps(payload, ensure_ascii=False))
        osess.add(plan)
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="PLAN_CREATED",
                         entity=f"{plan_type}_plan", entity_id=plan.id)
        return {"plan_id": plan.id, "status": plan.status, "version": 1}


def update_plan_draft(data_dir: Path, actor: int, *, office_id: int,
                      plan_type: str, plan_id: int, payload: dict) -> dict:
    """Edit a draft (or changes_requested -> back to draft). No revision yet."""
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        plan = _get_plan(osess, plan_type, plan_id)
        _require_own_or_reviewer(is_reviewer, actor, plan.representative_id)
        with dbm.identity_session(data_dir) as s2:
            _require_active_rep(s2, plan.representative_id, office_id)
        if plan.status == "changes_requested":
            _transition(plan, "draft")
        elif plan.status == "approved":
            # start a new version cycle of an approved plan
            _transition(plan, "superseded")
            _transition(plan, "draft")
        elif plan.status != "draft":
            raise ValidationError("only draft plans can be edited")
        plan.payload_json = json.dumps(payload, ensure_ascii=False)
        plan.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor, event_type="PLAN_UPDATED",
                         entity=f"{plan_type}_plan", entity_id=plan.id)
        return {"plan_id": plan.id, "status": plan.status}


def submit_plan(data_dir: Path, actor: int, *, office_id: int,
                plan_type: str, plan_id: int) -> dict:
    """Submit for review. Snapshots an immutable revision. New version if the
    previous one was approved (old revision marked superseded)."""
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        plan = _get_plan(osess, plan_type, plan_id)
        _require_own_or_reviewer(is_reviewer, actor, plan.representative_id)
        with dbm.identity_session(data_dir) as s2:
            _require_active_rep(s2, plan.representative_id, office_id)
        if plan.status not in ("draft", "changes_requested"):
            raise ValidationError("only draft plans can be submitted")
        if plan.status == "draft" and _was_approved(osess, plan_type, plan.id):
            # starting a new version of an approved plan
            plan.current_version += 1
            _mark_superseded(osess, plan_type, plan.id, plan.current_version)
        _transition(plan, "submitted")
        plan.submitted_at = utcnow()
        osess.add(om.PlanRevision(
            plan_type=plan_type, plan_id=plan.id, version=plan.current_version,
            payload_json=plan.payload_json, submitted_by=actor))
        log_office_event(osess, actor_user_id=actor, event_type="PLAN_SUBMITTED",
                         entity=f"{plan_type}_plan", entity_id=plan.id,
                         meta=json.dumps({"version": plan.current_version}))
        return {"plan_id": plan.id, "status": plan.status,
                "version": plan.current_version}


def review_plan(data_dir: Path, actor: int, *, office_id: int, plan_type: str,
                plan_id: int, decision: str, notes: str = "") -> dict:
    """Supervisor review: approve | request_changes | start_review."""
    if decision not in ("approve", "request_changes", "start_review"):
        raise ValidationError("unknown review decision")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "plan.review", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        plan = _get_plan(osess, plan_type, plan_id)
        if decision == "start_review":
            _transition(plan, "under_review")
        elif decision == "approve":
            _transition(plan, "approved")
        else:
            _transition(plan, "changes_requested")
        plan.reviewed_by = actor
        plan.reviewed_at = utcnow()
        plan.review_notes = notes
        log_office_event(osess, actor_user_id=actor, event_type="PLAN_REVIEWED",
                         entity=f"{plan_type}_plan", entity_id=plan.id,
                         reason=notes,
                         meta=json.dumps({"decision": decision}))
        return {"plan_id": plan.id, "status": plan.status}


def add_planned_visits(data_dir: Path, actor: int, *, office_id: int,
                       plan_type: str, plan_id: int, visits: list[dict]) -> int:
    """Attach planned visits to a draft plan."""
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        plan = _get_plan(osess, plan_type, plan_id)
        _require_own_or_reviewer(is_reviewer, actor, plan.representative_id)
        if plan.status != "draft":
            raise ValidationError("can only plan visits on a draft plan")
        n = 0
        for i, v in enumerate(visits):
            if v.get("target_type") not in ("doctor", "pharmacy"):
                raise ValidationError("target_type must be doctor or pharmacy")
            _require_target(osess, v["target_type"], v["target_id"])
            osess.add(om.PlannedVisit(
                plan_type=plan_type, plan_id=plan.id,
                target_type=v["target_type"], target_id=int(v["target_id"]),
                expected_order=int(v.get("expected_order", i)),
                expected_time=str(v.get("expected_time", "")),
                notes=str(v.get("notes", ""))))
            n += 1
        return n


def get_plan(data_dir: Path, actor: int, *, office_id: int,
             plan_type: str, plan_id: int) -> dict:
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        plan = _get_plan(osess, plan_type, plan_id)
        _require_own_or_reviewer(is_reviewer, actor, plan.representative_id)
        period_col = _PERIOD[plan_type]
        revisions = (osess.query(om.PlanRevision)
                     .filter_by(plan_type=plan_type, plan_id=plan.id)
                     .order_by(om.PlanRevision.version).all())
        visits = (osess.query(om.PlannedVisit)
                  .filter_by(plan_type=plan_type, plan_id=plan.id)
                  .order_by(om.PlannedVisit.expected_order).all())
        return {
            "plan_id": plan.id, "status": plan.status,
            "version": plan.current_version,
            "rep_id": plan.representative_id,
            "period": getattr(plan, period_col),
            "payload": json.loads(plan.payload_json or "{}"),
            "revisions": [{"version": r.version,
                           "submitted_at": r.submitted_at.isoformat(),
                           "superseded_by": r.superseded_by} for r in revisions],
            "planned_visits": [{"target_type": v.target_type,
                                "target_id": v.target_id,
                                "expected_time": v.expected_time} for v in visits],
        }


def list_plans(data_dir: Path, actor: int, *, office_id: int,
               plan_type: str = "weekly",
               status: str | None = None) -> list[dict]:
    """Reviewer view of plans (for the bot review queue)."""
    if plan_type not in _PLAN_MODELS:
        raise ValidationError("plan_type must be weekly or daily")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "plan.review", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        model = _PLAN_MODELS[plan_type]
        q = osess.query(model).order_by(model.id.desc())
        if status:
            q = q.filter_by(status=status)
        period_col = _PERIOD[plan_type]
        return [{"plan_id": p.id, "rep_id": p.representative_id,
                 "period": getattr(p, period_col), "status": p.status,
                 "version": p.current_version}
                for p in q.limit(50).all()]


def _was_approved(osess: Session, plan_type: str, plan_id: int) -> bool:
    return (osess.query(om.PlanRevision)
            .filter_by(plan_type=plan_type, plan_id=plan_id).count() > 0)


def _mark_superseded(osess: Session, plan_type: str, plan_id: int,
                     new_version: int) -> None:
    (osess.query(om.PlanRevision)
     .filter_by(plan_type=plan_type, plan_id=plan_id)
     .filter(om.PlanRevision.superseded_by.is_(None))
     .update({"superseded_by": new_version}))


def _require_target(osess: Session, target_type: str, target_id: int) -> None:
    model = om.Doctor if target_type == "doctor" else om.Pharmacy
    target = osess.get(model, int(target_id))
    if target is None or target.status != "active":
        raise NotFound(f"{target_type} not found or inactive")
