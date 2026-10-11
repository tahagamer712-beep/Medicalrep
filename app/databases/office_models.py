"""Per-office operational database models (data/office_{id}.db).

Phase 2 scope: representatives + office audit + schema version.
Plans/visits/payroll/surveys tables arrive in their phases.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.time_utils import utcnow
from app.core.types import UTCDateTime

OFFICE_SCHEMA_VERSION = 8


class OfficeBase(DeclarativeBase):
    pass


class SchemaVersion(OfficeBase):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Representative(OfficeBase):
    """Job record of a rep inside THIS office. user_id references
    identity.users.id (logical link, no cross-DB foreign key possible)."""

    __tablename__ = "representatives"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(index=True)
    employee_code: Mapped[str] = mapped_column(String(32), unique=True)
    supervisor_user_id: Mapped[int | None] = mapped_column(nullable=True)
    hire_date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD
    job_status: Mapped[str] = mapped_column(String(20), default="active")  # active|ended
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OfficeAuditEvent(OfficeBase):
    __tablename__ = "office_audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(nullable=True)
    result: Mapped[str] = mapped_column(String(16))  # ok|denied|failed
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class BackupRecord(OfficeBase):
    """Log of encrypted backups taken for this office (schema v2)."""

    __tablename__ = "backup_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    file_name: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column()
    encrypted: Mapped[int] = mapped_column(default=1)
    verified: Mapped[int] = mapped_column(default=0)
    restored_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# --- Phase 4: doctors / pharmacies -------------------------------------------

class Doctor(OfficeBase):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str] = mapped_column(String(100), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    area: Mapped[str] = mapped_column(String(100), default="")
    # --- Phase 5b: full doctor profile ---
    sub_area: Mapped[str] = mapped_column(String(100), default="")
    nearest_pharmacy_id: Mapped[int | None] = mapped_column(
        ForeignKey("pharmacies.id"), nullable=True)
    doctor_class: Mapped[str] = mapped_column(String(10), default="")
    # A | B | Z | A-B | "" (unspecified)
    additional_info: Mapped[str] = mapped_column(Text, default="")
    reception_days_json: Mapped[str] = mapped_column(Text, default="[]")
    # JSON list of weekday ints, Monday=0 .. Sunday=6
    non_reception_days_json: Mapped[str] = mapped_column(Text, default="[]")
    reception_time: Mapped[str] = mapped_column(String(100), default="")
    doctor_notes: Mapped[str] = mapped_column(Text, default="")
    dealer_info: Mapped[str] = mapped_column(Text, default="")
    assigned_rep_id: Mapped[int | None] = mapped_column(nullable=True)
    external_code: Mapped[str | None] = mapped_column(String(32), unique=True,
                                                     nullable=True)
    # stable id used by Excel import/export
    version: Mapped[int] = mapped_column(default=1)  # optimistic concurrency
    status: Mapped[str] = mapped_column(String(20), default="active")
    # active | inactive | left
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DoctorProduct(OfficeBase):
    """TGT items: many products per doctor, linked to the real catalog."""

    __tablename__ = "doctor_products"
    __table_args__ = (UniqueConstraint("doctor_id", "product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    added_by: Mapped[int] = mapped_column(default=0)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DoctorHistory(OfficeBase):
    """Field-level change history. Never deleted."""

    __tablename__ = "doctor_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    changed_by: Mapped[int] = mapped_column(default=0)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    field_name: Mapped[str] = mapped_column(String(60))
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")


class DoctorFieldSurvey(OfficeBase):
    """Time-varying market data per doctor. Append-only."""

    __tablename__ = "doctor_field_surveys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    rep_id: Mapped[int] = mapped_column(index=True)
    products_prices_json: Mapped[str] = mapped_column(Text, default="[]")
    competitors_json: Mapped[str] = mapped_column(Text, default="[]")
    market_notes: Mapped[str] = mapped_column(Text, default="")
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BaseSalaryChange(OfficeBase):
    """Base-salary history with effective dates. Append-only."""

    __tablename__ = "base_salary_changes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    representative_id: Mapped[int] = mapped_column(index=True)
    amount: Mapped[int] = mapped_column()
    effective_from: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_by: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ImportLog(OfficeBase):
    """Every Excel import: who, when, counts. file_hash UNIQUE = idempotency."""

    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))  # doctors
    file_name: Mapped[str] = mapped_column(String(160))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True)
    uploaded_by: Mapped[int] = mapped_column(default=0)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="applied")


class Pharmacy(OfficeBase):
    __tablename__ = "pharmacies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    area: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Phase 4: plans -----------------------------------------------------------
# States: draft -> submitted -> under_review -> approved | changes_requested
#         approved -> superseded (by a newer approved version)

PLAN_TRANSITIONS = {
    "draft": {"submitted"},
    "submitted": {"under_review", "draft"},       # reviewer can send back directly
    "under_review": {"approved", "changes_requested"},
    "changes_requested": {"draft"},
    "approved": {"superseded"},
    "superseded": {"draft"},                      # new version cycle begins
}


class WeeklyPlan(OfficeBase):
    __tablename__ = "weekly_plans"
    __table_args__ = (UniqueConstraint("representative_id", "week_start"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    representative_id: Mapped[int] = mapped_column(index=True)  # user_id
    week_start: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    current_version: Mapped[int] = mapped_column(default=1)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DailyPlan(OfficeBase):
    __tablename__ = "daily_plans"
    __table_args__ = (UniqueConstraint("representative_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    representative_id: Mapped[int] = mapped_column(index=True)  # user_id
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    current_version: Mapped[int] = mapped_column(default=1)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PlanRevision(OfficeBase):
    """Immutable snapshot of a plan at submit time. Never updated/deleted."""

    __tablename__ = "plan_revisions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_type: Mapped[str] = mapped_column(String(10), index=True)  # weekly|daily
    plan_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int] = mapped_column()
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    submitted_by: Mapped[int] = mapped_column()
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    superseded_by: Mapped[int | None] = mapped_column(nullable=True)  # version


class PlannedVisit(OfficeBase):
    __tablename__ = "planned_visits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_type: Mapped[str] = mapped_column(String(10))  # weekly|daily
    plan_id: Mapped[int] = mapped_column(index=True)
    target_type: Mapped[str] = mapped_column(String(10))  # doctor|pharmacy
    target_id: Mapped[int] = mapped_column()
    expected_order: Mapped[int] = mapped_column(default=0)
    expected_time: Mapped[str] = mapped_column(String(5), default="")  # HH:MM
    notes: Mapped[str] = mapped_column(Text, default="")


# --- Phase 4: actual visits (strictly separate from planned) --------------------

class ActualVisit(OfficeBase):
    __tablename__ = "actual_visits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    representative_id: Mapped[int] = mapped_column(index=True)  # user_id
    target_type: Mapped[str] = mapped_column(String(10))  # doctor|pharmacy
    target_id: Mapped[int] = mapped_column()
    visited_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    visit_type: Mapped[str] = mapped_column(String(30), default="routine")
    outcome: Mapped[str] = mapped_column(String(30), default="done")
    notes: Mapped[str] = mapped_column(Text, default="")
    products_json: Mapped[str] = mapped_column(Text, default="[]")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="recorded")
    correction_of: Mapped[int | None] = mapped_column(nullable=True)
    created_by: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Phase 4: attendance ---------------------------------------------------------

class AttendanceRecord(OfficeBase):
    __tablename__ = "attendance_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    representative_id: Mapped[int] = mapped_column(index=True)  # user_id
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    check_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    check_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="present")
    late_minutes: Mapped[int] = mapped_column(default=0)
    corrected_by: Mapped[int | None] = mapped_column(nullable=True)
    correction_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Phase 4: scheduled notification jobs (DB-backed, restart-safe) --------------

class NotificationJob(OfficeBase):
    __tablename__ = "notification_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    last_error: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Phase 5: products / leave / meetings / holidays / surveys -----------------

class ProductUnit(OfficeBase):
    """Catalog of units (box, strip, tablet...). Shared per office."""

    __tablename__ = "product_units"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")


class Product(OfficeBase):
    """Product catalog per office."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_name: Mapped[str] = mapped_column(String(120), index=True)
    scientific_name: Mapped[str] = mapped_column(String(120), default="")
    manufacturer: Mapped[str] = mapped_column(String(120), default="")
    dosage_form: Mapped[str] = mapped_column(String(60), default="")
    strength: Mapped[str] = mapped_column(String(60), default="")
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("product_units.id"),
                                               nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active|inactive
    created_by: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LeaveType(OfficeBase):
    __tablename__ = "leave_types"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    default_days_per_year: Mapped[int] = mapped_column(default=0)
    requires_attachment: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(12), default="active")


LEAVE_TRANSITIONS = {
    "pending": {"approved", "rejected", "cancelled"},
    "approved": {"cancelled"},
    "rejected": set(),
    "cancelled": set(),
}


class LeaveRequest(OfficeBase):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    representative_id: Mapped[int] = mapped_column(index=True)
    leave_type_id: Mapped[int] = mapped_column(ForeignKey("leave_types.id"))
    start_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    end_date: Mapped[str] = mapped_column(String(10))
    days_count: Mapped[int] = mapped_column(default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    attachment_path: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    reviewed_by: Mapped[int | None] = mapped_column(nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Meeting(OfficeBase):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))
    meeting_type: Mapped[str] = mapped_column(String(30), default="regular")
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    location: Mapped[str] = mapped_column(String(160), default="")
    agenda: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="scheduled", index=True)
    created_by: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MeetingAttendee(OfficeBase):
    __tablename__ = "meeting_attendees"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    user_id: Mapped[int] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(12), default="invited")
    marked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Holiday(OfficeBase):
    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), unique=True)  # YYYY-MM-DD
    name: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[int] = mapped_column(default=0)


class Survey(OfficeBase):
    __tablename__ = "surveys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    valid_from: Mapped[str] = mapped_column(String(10), default="")
    valid_to: Mapped[str] = mapped_column(String(10), default="")
    created_by: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SurveyQuestion(OfficeBase):
    __tablename__ = "survey_questions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    qtype: Mapped[str] = mapped_column(String(20), default="text")
    # text | single_choice | rating
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    required: Mapped[bool] = mapped_column(default=True)
    position: Mapped[int] = mapped_column(default=0)


class SurveyResponse(OfficeBase):
    __tablename__ = "survey_responses"
    __table_args__ = (UniqueConstraint("survey_id", "respondent_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id"), index=True)
    respondent_id: Mapped[int] = mapped_column(index=True)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SurveyAnswer(OfficeBase):
    __tablename__ = "survey_answers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    response_id: Mapped[int] = mapped_column(ForeignKey("survey_responses.id"),
                                            index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("survey_questions.id"))
    answer_json: Mapped[str] = mapped_column(Text, default="null")


class Attachment(OfficeBase):
    """File metadata. Files live under data/<office_id>/attachments/ with
    uuid-based stored names. Soft-deleted rows are hidden but kept."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(30), index=True)
    owner_id: Mapped[int] = mapped_column(index=True)
    file_name: Mapped[str] = mapped_column(String(160))
    stored_path: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(80), default="")
    size_bytes: Mapped[int] = mapped_column(default=0)
    uploaded_by: Mapped[int] = mapped_column(default=0)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted: Mapped[bool] = mapped_column(default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, default=None)


# --- Phase 6: payroll -------------------------------------------------------
# Money is integer IQD. The ledger is append-only: amounts are never edited;
# corrections are new reversal rows. Every transaction carries a unique uuid
# for idempotency (double-submits never double-apply).

class PayrollPeriod(OfficeBase):
    __tablename__ = "payroll_periods"
    __table_args__ = (UniqueConstraint("year", "month"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    year: Mapped[int] = mapped_column()
    month: Mapped[int] = mapped_column()
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    # open | finalized
    created_by: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SalaryRecord(OfficeBase):
    __tablename__ = "salary_records"
    __table_args__ = (UniqueConstraint("payroll_period_id", "representative_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payroll_period_id: Mapped[int] = mapped_column(ForeignKey("payroll_periods.id"),
                                                  index=True)
    representative_id: Mapped[int] = mapped_column(index=True)
    base_salary: Mapped[int] = mapped_column(default=0)
    total_bonuses: Mapped[int] = mapped_column(default=0)
    total_deductions: Mapped[int] = mapped_column(default=0)
    net_salary: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    finalized_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


TXN_TRANSITIONS = {
    "pending": {"applied", "rejected"},
    "applied": set(),
    "rejected": set(),
}


class FinancialTransaction(OfficeBase):
    __tablename__ = "financial_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    representative_id: Mapped[int] = mapped_column(index=True)
    payroll_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("payroll_periods.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    # deduction_direct | deduction_proposed | bonus | correction
    signed_amount: Mapped[int] = mapped_column()  # negative = deduction
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    # pending | applied | rejected
    reason: Mapped[str] = mapped_column(Text, default="")
    related_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[int] = mapped_column(default=0)
    approved_by: Mapped[int | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Phase 10: catalog product links -------------------------------------------
# Structured links from visits / field surveys to the product catalog.
# Legacy free-text JSON columns (ActualVisit.products_json,
# DoctorFieldSurvey.products_prices_json) are kept for backward compatibility
# and mirrored with structured entries when catalog links are recorded.


class VisitProduct(OfficeBase):
    """Catalog products discussed/sampled during a visit."""

    __tablename__ = "visit_products"
    __table_args__ = (UniqueConstraint("visit_id", "product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("actual_visits.id"),
                                         index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(default=1)
    added_by: Mapped[int] = mapped_column(default=0)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FieldSurveyProduct(OfficeBase):
    """Catalog product prices observed in a doctor field survey."""

    __tablename__ = "field_survey_products"
    __table_args__ = (UniqueConstraint("field_survey_id", "product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    field_survey_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_field_surveys.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    price: Mapped[str] = mapped_column(String(60), default="")
    added_by: Mapped[int] = mapped_column(default=0)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
