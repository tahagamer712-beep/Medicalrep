"""Server-side permission enforcement.

Every service function MUST call require_permission() before touching data.
Permissions are resolved from the actor's ACTIVE memberships in the central
identity DB — never from names/ids typed by the user, never from UI hiding.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import NotFound, PermissionDenied
from app.identity import models as im

ROLE_LEVEL = {"rep": 1, "supervisor": 2, "manager": 3, "owner": 4}

# action -> (minimum role level, scope)
# scope "global": owner only in practice (level 4)
# scope "company": membership.company_id must match
# scope "office": membership.office_id must match, or manager of the company
ACTIONS: dict[str, tuple[int, str]] = {
    "company.create": (4, "global"),
    "company.manage": (4, "global"),
    "office.create": (4, "global"),
    "office.manage": (2, "office"),
    "rep.manage": (2, "office"),
    "rep.self": (1, "office"),
    "user.create": (3, "company"),
    "audit.read": (2, "office"),
    # Phase 4
    "plan.own": (1, "office"),        # rep: own plans (+supervisor override in service)
    "plan.review": (2, "office"),    # supervisor+: review team plans
    "visit.record": (1, "office"),   # rep: record own visits
    "visit.correct": (2, "office"),  # supervisor+: correct visits
    "attendance.record": (1, "office"),
    "attendance.correct": (2, "office"),
    "directory.manage": (2, "office"),  # doctors/pharmacies
    # Phase 5
    "leave.request": (1, "office"),   # rep: own leave requests
    "leave.review": (2, "office"),   # supervisor+: approve/reject
    "meeting.manage": (2, "office"),
    "survey.manage": (2, "office"),
    "survey.respond": (1, "office"),
    "product.manage": (2, "office"),
    "holiday.manage": (3, "office"),  # manager+
    # Phase 6 — finance
    "payroll.manage": (3, "office"),     # manager+: periods, base salary, finalize
    "deduction.direct": (2, "office"),   # supervisor+: immediate deduction
    "deduction.propose": (2, "office"),   # supervisor+/bot: proposal (no effect)
    "deduction.review": (2, "office"),    # supervisor+: approve/reject proposals
    "bonus.grant": (2, "office"),         # supervisor+: immediate bonus
    "transaction.reverse": (2, "office"),  # supervisor+: reversal (new row)
    # Phase 7 — reports
    "report.view": (2, "office"),  # supervisor+: office reports (reps see own)
}


def active_memberships(s: Session, user_id: int) -> list[im.Membership]:
    return (
        s.query(im.Membership)
        .filter(im.Membership.user_id == user_id, im.Membership.status == "active")
        .all()
    )


def require_permission(
    s: Session,
    actor_user_id: int,
    action: str,
    *,
    company_id: int | None = None,
    office_id: int | None = None,
) -> im.Membership:
    """Return the granting membership, or raise PermissionDenied/NotFound."""
    if action not in ACTIONS:
        raise PermissionDenied(f"unknown action '{action}'")

    user = s.get(im.User, actor_user_id)
    if user is None or user.status != "active":
        raise PermissionDenied("unknown or inactive user")

    memberships = active_memberships(s, actor_user_id)
    if not memberships:
        raise PermissionDenied("user has no active membership")

    # Owner: any active owner membership grants everything (audited by caller).
    for m in memberships:
        if m.role == "owner":
            return m

    min_level, scope = ACTIONS[action]

    if office_id is not None:
        office = s.get(im.Office, office_id)
        if office is None:
            raise NotFound("office not found")
        company_id = office.company_id

    if scope in ("company", "office") and company_id is None:
        raise PermissionDenied("action requires a company/office scope")

    for m in memberships:
        if ROLE_LEVEL.get(m.role, 0) < min_level:
            continue
        if scope == "company" and m.company_id == company_id:
            return m
        if scope == "office":
            if m.office_id is not None and m.office_id == office_id:
                return m
            # manager of the office's company covers office-scoped actions
            if m.role == "manager" and m.company_id == company_id:
                return m

    raise PermissionDenied(f"action '{action}' not permitted for this user")


def require_owner(s: Session, actor_user_id: int) -> im.Membership:
    """Shortcut for owner-only actions."""
    return require_permission(s, actor_user_id, "company.create")
