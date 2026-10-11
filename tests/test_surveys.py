"""Surveys: draft -> active -> closed, responses, results."""
import pytest

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.services import surveys as sv


@pytest.fixture()
def active_survey(seed):
    d = seed["data_dir"]
    s = sv.create_survey(d, seed["supervisor1"], office_id=seed["office1"],
                         title="رضا الأطباء")
    q1 = sv.add_question(d, seed["supervisor1"], office_id=seed["office1"],
                         survey_id=s["survey_id"], text="كيف كان التفاعل؟",
                         qtype="rating")
    q2 = sv.add_question(d, seed["supervisor1"], office_id=seed["office1"],
                         survey_id=s["survey_id"], text="المنتج المفضل؟",
                         qtype="single_choice", options=["أ", "ب"])
    q3 = sv.add_question(d, seed["supervisor1"], office_id=seed["office1"],
                         survey_id=s["survey_id"], text="ملاحظات", qtype="text",
                         required=False)
    sv.set_survey_status(d, seed["supervisor1"], office_id=seed["office1"],
                         survey_id=s["survey_id"], status="active")
    return s["survey_id"], q1["question_id"], q2["question_id"], q3["question_id"]


def test_respond_and_results(seed, active_survey):
    d = seed["data_dir"]
    sid, q1, q2, q3 = active_survey
    sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                       survey_id=sid, answers={q1: 4, q2: "أ", q3: "جيد"})
    sv.submit_response(d, seed["rep2_o1"], office_id=seed["office1"],
                       survey_id=sid, answers={q1: 5, q2: "ب"})
    res = sv.survey_results(d, seed["supervisor1"], office_id=seed["office1"],
                            survey_id=sid)
    assert res["questions"][0]["average"] == 4.5
    assert res["questions"][1]["counts"] == {"أ": 1, "ب": 1}


def test_duplicate_response_rejected(seed, active_survey):
    d = seed["data_dir"]
    sid, q1, q2, q3 = active_survey
    sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                       survey_id=sid, answers={q1: 3, q2: "أ"})
    with pytest.raises(AlreadyExists):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=sid, answers={q1: 5, q2: "ب"})


def test_required_and_type_validation(seed, active_survey):
    d = seed["data_dir"]
    sid, q1, q2, q3 = active_survey
    with pytest.raises(ValidationError):  # missing required q1
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=sid, answers={q2: "أ"})
    with pytest.raises(ValidationError):  # bad choice
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=sid, answers={q1: 3, q2: "ج"})
    with pytest.raises(ValidationError):  # rating out of range
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=sid, answers={q1: 9, q2: "أ"})


def test_cannot_activate_without_questions(seed):
    d = seed["data_dir"]
    s = sv.create_survey(d, seed["supervisor1"], office_id=seed["office1"],
                         title="فارغ")
    with pytest.raises(ValidationError):
        sv.set_survey_status(d, seed["supervisor1"], office_id=seed["office1"],
                             survey_id=s["survey_id"], status="active")


def test_cannot_edit_after_activation(seed, active_survey):
    d = seed["data_dir"]
    sid = active_survey[0]
    with pytest.raises(ValidationError):
        sv.add_question(d, seed["supervisor1"], office_id=seed["office1"],
                        survey_id=sid, text="سؤال متأخر")


def test_cannot_respond_to_draft(seed):
    d = seed["data_dir"]
    s = sv.create_survey(d, seed["supervisor1"], office_id=seed["office1"],
                         title="مسودة")
    with pytest.raises(ValidationError):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=s["survey_id"], answers={})


def test_rep_cannot_create_survey(seed):
    d = seed["data_dir"]
    with pytest.raises(PermissionDenied):
        sv.create_survey(d, seed["rep1_o1"], office_id=seed["office1"],
                         title="x")


def test_cross_office_survey_isolation(seed, active_survey):
    d = seed["data_dir"]
    sid, q1, *_ = active_survey
    # rep from office 2 has no scope in office 1
    with pytest.raises(PermissionDenied):
        sv.submit_response(d, seed["rep_o2"], office_id=seed["office1"],
                           survey_id=sid, answers={q1: 4})
    # office 2 sees no surveys
    assert sv.list_surveys(d, seed["supervisor2"],
                           office_id=seed["office2"]) == []


def test_close_survey(seed, active_survey):
    d = seed["data_dir"]
    sid, q1, q2, q3 = active_survey
    sv.set_survey_status(d, seed["supervisor1"], office_id=seed["office1"],
                         survey_id=sid, status="closed")
    with pytest.raises(ValidationError):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=sid, answers={q1: 4, q2: "أ"})
