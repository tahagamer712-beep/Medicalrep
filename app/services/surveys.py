"""Configurable surveys: draft -> active -> closed.

- Questions: text | single_choice | rating (1-5).
- One response per respondent per survey (unique constraint).
- Attachments are metadata only in Phase 5 (file upload wiring comes later).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event

QTYPES = ("text", "single_choice", "rating")


def create_survey(data_dir: Path, actor: int, *, office_id: int, title: str,
                  description: str = "", valid_from: str = "",
                  valid_to: str = "") -> dict:
    if not title.strip():
        raise ValidationError("survey title is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "survey.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        sv = om.Survey(title=title.strip(), description=description,
                       valid_from=valid_from, valid_to=valid_to,
                       created_by=actor)
        osess.add(sv)
        osess.flush()
        return {"survey_id": sv.id, "status": sv.status}


def add_question(data_dir: Path, actor: int, *, office_id: int, survey_id: int,
                 text: str, qtype: str = "text",
                 options: list[str] | None = None,
                 required: bool = True) -> dict:
    if qtype not in QTYPES:
        raise ValidationError(f"qtype must be one of {QTYPES}")
    if qtype == "single_choice" and not options:
        raise ValidationError("single_choice needs options")
    if not text.strip():
        raise ValidationError("question text is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "survey.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        sv = osess.get(om.Survey, survey_id)
        if sv is None:
            raise NotFound("survey not found")
        if sv.status != "draft":
            raise ValidationError("can only edit a draft survey")
        pos = osess.query(om.SurveyQuestion).filter_by(survey_id=survey_id).count()
        q = om.SurveyQuestion(
            survey_id=survey_id, text=text.strip(), qtype=qtype,
            options_json=json.dumps(options or [], ensure_ascii=False),
            required=bool(required), position=pos)
        osess.add(q)
        osess.flush()
        return {"question_id": q.id, "qtype": qtype}


def set_survey_status(data_dir: Path, actor: int, *, office_id: int,
                      survey_id: int, status: str) -> dict:
    if status not in ("active", "closed"):
        raise ValidationError("status must be active or closed")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "survey.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        sv = osess.get(om.Survey, survey_id)
        if sv is None:
            raise NotFound("survey not found")
        if status == "active":
            if sv.status != "draft":
                raise ValidationError("only drafts can be activated")
            if osess.query(om.SurveyQuestion).filter_by(survey_id=survey_id).count() == 0:
                raise ValidationError("cannot activate a survey with no questions")
        if status == "closed" and sv.status != "active":
            raise ValidationError("only active surveys can be closed")
        sv.status = status
        log_office_event(osess, actor_user_id=actor,
                         event_type="SURVEY_" + status.upper(),
                         entity="survey", entity_id=sv.id)
        return {"survey_id": sv.id, "status": sv.status}


def submit_response(data_dir: Path, actor: int, *, office_id: int,
                    survey_id: int, answers: dict[int, object]) -> dict:
    with dbm.identity_session(data_dir) as s:
        try:
            require_permission(s, actor, "survey.manage", office_id=office_id)
        except PermissionDenied:
            require_permission(s, actor, "survey.respond", office_id=office_id)
            m = (s.query(im.Membership)
                 .filter_by(user_id=actor, office_id=office_id, role="rep",
                            status="active").first())
            if m is None:
                raise PermissionDenied("only active reps can respond")
    with dbm.office_session(data_dir, office_id) as osess:
        sv = osess.get(om.Survey, survey_id)
        if sv is None:
            raise NotFound("survey not found")
        if sv.status != "active":
            raise ValidationError("survey is not active")
        today = date.today().isoformat()
        if sv.valid_from and today < sv.valid_from:
            raise ValidationError("survey has not started yet")
        if sv.valid_to and today > sv.valid_to:
            raise ValidationError("survey is closed")
        questions = (osess.query(om.SurveyQuestion)
                     .filter_by(survey_id=survey_id)
                     .order_by(om.SurveyQuestion.position).all())
        if osess.query(om.SurveyResponse).filter_by(
                survey_id=survey_id, respondent_id=actor).first():
            raise AlreadyExists("already responded to this survey")
        _validate_answers(questions, answers)
        resp = om.SurveyResponse(survey_id=survey_id, respondent_id=actor)
        osess.add(resp)
        osess.flush()
        for q in questions:
            osess.add(om.SurveyAnswer(
                response_id=resp.id, question_id=q.id,
                answer_json=json.dumps(answers.get(q.id), ensure_ascii=False)))
        log_office_event(osess, actor_user_id=actor,
                         event_type="SURVEY_RESPONDED",
                         entity="survey", entity_id=survey_id)
        return {"response_id": resp.id}


def _validate_answers(questions: list, answers: dict) -> None:
    for q in questions:
        val = answers.get(q.id)
        if q.required and val in (None, "", []):
            raise ValidationError(f"question {q.id} is required")
        if val in (None, "", []):
            continue
        if q.qtype == "single_choice":
            opts = json.loads(q.options_json)
            if val not in opts:
                raise ValidationError(f"invalid choice for question {q.id}")
        elif q.qtype == "rating":
            if not isinstance(val, int) or not 1 <= val <= 5:
                raise ValidationError(f"rating must be 1-5 for question {q.id}")


def survey_results(data_dir: Path, actor: int, *, office_id: int,
                   survey_id: int) -> dict:
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "survey.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        sv = osess.get(om.Survey, survey_id)
        if sv is None:
            raise NotFound("survey not found")
        questions = (osess.query(om.SurveyQuestion)
                     .filter_by(survey_id=survey_id)
                     .order_by(om.SurveyQuestion.position).all())
        out = {"survey_id": survey_id, "title": sv.title, "questions": []}
        for q in questions:
            rows = (osess.query(om.SurveyAnswer)
                    .filter_by(question_id=q.id).all())
            vals = [json.loads(r.answer_json) for r in rows
                    if json.loads(r.answer_json) not in (None, "", [])]
            summary: dict = {"question_id": q.id, "text": q.text,
                             "qtype": q.qtype, "responses": len(vals)}
            if q.qtype == "rating" and vals:
                summary["average"] = round(sum(vals) / len(vals), 2)
            elif q.qtype == "single_choice":
                counts: dict[str, int] = {}
                for v in vals:
                    counts[str(v)] = counts.get(str(v), 0) + 1
                summary["counts"] = counts
            else:
                summary["answers"] = vals
            out["questions"].append(summary)
        return out


def list_surveys(data_dir: Path, actor: int, *, office_id: int,
                 active_only: bool = True) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        m = (s.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, status="active").first())
        if m is None:
            raise PermissionDenied("no active membership in this office")
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.Survey).order_by(om.Survey.id.desc())
        if active_only:
            q = q.filter_by(status="active")
        return [{"survey_id": s.id, "title": s.title, "status": s.status}
                for s in q.all()]
