"""Catalog product links: visits, field surveys, configurable surveys.

All on dummy seed data. Legacy free-text JSON columns keep working.
"""
import json

import pytest

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import doctors as dsvc
from app.services import products as prod
from app.services import surveys as sv
from app.services import visits as vsvc


@pytest.fixture()
def catalog(seed):
    d = seed["data_dir"]
    sup = seed["supervisor1"]
    o1 = seed["office1"]
    p1 = prod.create_product(d, sup, office_id=o1, trade_name="أموكسيل",
                             scientific_name="Amoxicillin")["id"]
    p2 = prod.create_product(d, sup, office_id=o1, trade_name="بنادول",
                             scientific_name="Paracetamol")["id"]
    return {"p1": p1, "p2": p2}


@pytest.fixture()
def doctor(seed):
    d = seed["data_dir"]
    return vsvc.add_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                           name="د. رابط", specialty="باطنية",
                           phone="07222222222")["id"]


@pytest.fixture()
def visit(seed, doctor):
    d = seed["data_dir"]
    return vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=doctor,
        products=["دواء حر قديم"], confirm=True)["visit_id"]


def test_record_visit_with_catalog_products(seed, catalog, visit):
    d = seed["data_dir"]
    out = vsvc.record_visit_products(
        d, seed["rep1_o1"], office_id=seed["office1"], visit_id=visit,
        items=[{"product_id": catalog["p1"], "quantity": 3},
               {"product_id": catalog["p2"]}])
    assert out["visit_id"] == visit
    assert {p["product_id"] for p in out["products"]} == {catalog["p1"],
                                                         catalog["p2"]}
    rows = vsvc.list_visit_products(d, seed["rep1_o1"],
                                    office_id=seed["office1"], visit_id=visit)
    assert len(rows) == 2
    assert rows[0]["trade_name"] == "أموكسيل"
    assert rows[0]["quantity"] == 3


def test_visit_product_other_office_rejected(seed, catalog, visit):
    # IDs are office-scoped (separate DB files): create products in office2
    # and pick an id that does not exist in office1's catalog.
    d = seed["data_dir"]
    for i in range(3):
        prod.create_product(d, seed["supervisor2"],
                            office_id=seed["office2"],
                            trade_name=f"دواء البصرة {i}")
    with pytest.raises(NotFound):
        vsvc.record_visit_products(
            d, seed["rep1_o1"], office_id=seed["office1"], visit_id=visit,
            items=[{"product_id": 3, "quantity": 1}])


def test_visit_product_inactive_rejected(seed, catalog, visit):
    d = seed["data_dir"]
    prod.set_product_status(d, seed["supervisor1"], office_id=seed["office1"],
                            product_id=catalog["p1"], active=False)
    with pytest.raises(ValidationError, match="not active"):
        vsvc.record_visit_products(
            d, seed["rep1_o1"], office_id=seed["office1"], visit_id=visit,
            items=[{"product_id": catalog["p1"], "quantity": 1}])


def test_visit_product_permission(seed, catalog, visit):
    d = seed["data_dir"]
    # rep from another office cannot link products to this visit
    with pytest.raises(PermissionDenied):
        vsvc.record_visit_products(
            d, seed["rep_o2"], office_id=seed["office1"], visit_id=visit,
            items=[{"product_id": catalog["p1"], "quantity": 1}])
    # supervisor of the office can (manages team visits)
    out = vsvc.record_visit_products(
        d, seed["supervisor1"], office_id=seed["office1"], visit_id=visit,
        items=[{"product_id": catalog["p1"], "quantity": 1}])
    assert out["products"][0]["quantity"] == 1


def test_legacy_products_json_preserved(seed, catalog, visit):
    d = seed["data_dir"]
    vsvc.record_visit_products(
        d, seed["rep1_o1"], office_id=seed["office1"], visit_id=visit,
        items=[{"product_id": catalog["p1"], "quantity": 2}])
    with dbm.office_session(d, seed["office1"]) as osess:
        v = osess.get(om.ActualVisit, visit)
        entries = json.loads(v.products_json)
    # legacy free-text entry kept, structured entry added
    assert "دواء حر قديم" in entries
    structured = [e for e in entries
                  if isinstance(e, dict) and "product_id" in e]
    assert len(structured) == 1
    assert structured[0]["trade_name"] == "أموكسيل"
    assert structured[0]["quantity"] == 2
    # re-linking same product updates quantity, no duplicates
    vsvc.record_visit_products(
        d, seed["rep1_o1"], office_id=seed["office1"], visit_id=visit,
        items=[{"product_id": catalog["p1"], "quantity": 5}])
    rows = vsvc.list_visit_products(d, seed["rep1_o1"],
                                    office_id=seed["office1"], visit_id=visit)
    assert len(rows) == 1 and rows[0]["quantity"] == 5


def test_field_survey_product_links(seed, catalog, doctor):
    d = seed["data_dir"]
    out = dsvc.add_field_survey(
        d, seed["supervisor1"], office_id=seed["office1"], doctor_id=doctor,
        products_prices=[{"name": "دواء حر", "price": "5000"}],
        product_items=[{"product_id": catalog["p1"], "price": "12000"},
                       {"product_id": catalog["p2"], "price": "8000"}])
    assert out["survey_id"] > 0
    rows = dsvc.list_field_survey_products(
        d, seed["supervisor1"], office_id=seed["office1"],
        survey_id=out["survey_id"])
    assert len(rows) == 2
    assert rows[0]["trade_name"] == "أموكسيل"
    assert rows[0]["price"] == "12000"
    # legacy JSON kept
    with dbm.office_session(d, seed["office1"]) as osess:
        fs = osess.get(om.DoctorFieldSurvey, out["survey_id"])
        legacy = json.loads(fs.products_prices_json)
    assert legacy == [{"name": "دواء حر", "price": "5000"}]


def test_field_survey_product_validation(seed, catalog, doctor):
    d = seed["data_dir"]
    for i in range(3):
        prod.create_product(d, seed["supervisor2"],
                            office_id=seed["office2"],
                            trade_name=f"دواء بصرة ميداني {i}")
    other = 3  # exists only in office2's catalog
    with pytest.raises(NotFound):
        dsvc.add_field_survey(
            d, seed["supervisor1"], office_id=seed["office1"],
            doctor_id=doctor,
            product_items=[{"product_id": other, "price": "1"}])
    prod.set_product_status(d, seed["supervisor1"], office_id=seed["office1"],
                            product_id=catalog["p2"], active=False)
    with pytest.raises(ValidationError, match="not active"):
        dsvc.add_field_survey(
            d, seed["supervisor1"], office_id=seed["office1"],
            doctor_id=doctor,
            product_items=[{"product_id": catalog["p2"], "price": "1"}])


def _active_survey_with_product_question(seed):
    d = seed["data_dir"]
    sup = seed["supervisor1"]
    o1 = seed["office1"]
    s = sv.create_survey(d, sup, office_id=o1, title="مسح منتجات")["survey_id"]
    q = sv.add_question(d, sup, office_id=o1, survey_id=s,
                        text="أي منتج تم عرضه؟",
                        qtype="product_select")["question_id"]
    sv.set_survey_status(d, sup, office_id=o1, survey_id=s, status="active")
    return s, q


def test_survey_product_select(seed, catalog):
    d = seed["data_dir"]
    s, q = _active_survey_with_product_question(seed)
    out = sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                             survey_id=s, answers={q: catalog["p1"]})
    assert out["response_id"] > 0
    res = sv.survey_results(d, seed["supervisor1"], office_id=seed["office1"],
                            survey_id=s)
    assert res["questions"][0]["counts"] == {"أموكسيل": 1}


def test_survey_product_select_rejects_bad_ids(seed, catalog):
    d = seed["data_dir"]
    s, q = _active_survey_with_product_question(seed)
    for i in range(3):
        prod.create_product(d, seed["supervisor2"],
                            office_id=seed["office2"],
                            trade_name=f"دواء بصرة استبيان {i}")
    with pytest.raises(NotFound):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=s, answers={q: 3})  # office2-only id
    with pytest.raises(NotFound):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=s, answers={q: 999999})
    prod.set_product_status(d, seed["supervisor1"], office_id=seed["office1"],
                            product_id=catalog["p1"], active=False)
    with pytest.raises(ValidationError, match="not active"):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=s, answers={q: catalog["p1"]})
    with pytest.raises(ValidationError):
        sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                           survey_id=s, answers={q: "not-an-id"})


def test_survey_product_select_multi(seed, catalog):
    d = seed["data_dir"]
    s, q = _active_survey_with_product_question(seed)
    sv.submit_response(d, seed["rep1_o1"], office_id=seed["office1"],
                       survey_id=s,
                       answers={q: [catalog["p1"], catalog["p2"]]})
    res = sv.survey_results(d, seed["supervisor1"], office_id=seed["office1"],
                            survey_id=s)
    assert res["questions"][0]["counts"] == {"أموكسيل": 1, "بنادول": 1}
