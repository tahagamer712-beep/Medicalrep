"""Doctor profiles: full fields, TGT products, history, surveys, search."""
import pytest

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.services import doctors as docs
from app.services import products as prod
from app.services import visits as vsvc


@pytest.fixture()
def full_doc(seed):
    d = seed["data_dir"]
    ph = vsvc.add_pharmacy(d, seed["supervisor1"], office_id=seed["office1"],
                           name="صيدلية الشفاء")
    u = prod.create_unit(d, seed["supervisor1"], office_id=seed["office1"],
                         name="علبة")
    p1 = prod.create_product(d, seed["supervisor1"], office_id=seed["office1"],
                             trade_name="Panadol", unit_id=u["id"])
    p2 = prod.create_product(d, seed["supervisor1"], office_id=seed["office1"],
                             trade_name="Brufen", unit_id=u["id"])
    out = docs.create_doctor(
        d, seed["supervisor1"], office_id=seed["office1"],
        name="د. أحمد حسن", area="المنصور", sub_area="شارع الأميرات",
        nearest_pharmacy_id=ph["id"], specialty="باطنية", doctor_class="A",
        additional_info="يفضل الزيارة صباحًا",
        reception_days_json="[0, 2]", non_reception_days_json="[4]",
        reception_time="09:00-12:00", doctor_notes="متعاون",
        assigned_rep_id=seed["rep1_o1"])
    docs.set_tgt_products(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=out["id"], product_ids=[p1["id"], p2["id"]])
    return out["id"], p1["id"], p2["id"]


def test_full_profile_roundtrip(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    got = docs.get_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=did)
    assert got["area"] == "المنصور"
    assert got["sub_area"] == "شارع الأميرات"
    assert got["doctor_class"] == "A"
    assert got["reception_days"] == [0, 2]
    assert got["non_reception_days"] == [4]
    assert got["assigned_rep_id"] == seed["rep1_o1"]
    assert len(got["tgt_products"]) == 2
    assert got["status"] == "active"


def test_invalid_class_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        docs.create_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                           name="د. س", doctor_class="X")


def test_similar_name_warning(seed, full_doc):
    d = seed["data_dir"]
    out = docs.create_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                             name="د. احمد حسن")  # no shadda, similar
    assert len(out["similar_warnings"]) >= 1
    assert out["similar_warnings"][0]["name"] == "د. أحمد حسن"


def test_version_conflict_detected(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    v1 = docs.get_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                         doctor_id=did)["version"]
    docs.update_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                       doctor_id=did, expected_version=v1, area="اليرموك")
    with pytest.raises(ValidationError):
        docs.update_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                           doctor_id=did, expected_version=v1, area="الكاظمية")


def test_history_logged(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    v = docs.get_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                        doctor_id=did)["version"]
    docs.update_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                       doctor_id=did, expected_version=v, area="اليرموك",
                       reason="نقل عيادة")
    got = docs.get_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=did)
    fields = [h["field"] for h in got["history"]]
    assert "area" in fields


def test_status_left_keeps_history(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    out = docs.set_doctor_status(d, seed["supervisor1"],
                                 office_id=seed["office1"], doctor_id=did,
                                 status="left", reason="غادر المنطقة")
    assert out["status"] == "left"
    # still readable, history intact
    got = docs.get_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=did)
    assert got["status"] == "left" and len(got["history"]) > 0


def test_field_survey_append_only(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    docs.add_field_survey(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=did,
                          products_prices=[{"product": "Panadol",
                                            "price": 5000}],
                          competitors=[{"company": "X", "product": "Y"}],
                          market_notes="طلب مرتفع")
    docs.add_field_survey(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=did, market_notes="متابعة")
    got = docs.get_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                          doctor_id=did)
    assert got["last_survey"]["market_notes"] == "متابعة"


def test_tgt_unknown_product_rejected(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    with pytest.raises(NotFound):
        docs.set_tgt_products(d, seed["supervisor1"], office_id=seed["office1"],
                              doctor_id=did, product_ids=[99999])


def test_search_filters(seed, full_doc):
    d = seed["data_dir"]
    did, p1, _ = full_doc
    docs.create_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                       name="د. سارة", area="اليرموك", specialty="أطفال",
                       doctor_class="B")
    assert len(docs.search_doctors(d, seed["supervisor1"],
                                   office_id=seed["office1"],
                                   area="المنصور")) == 1
    assert len(docs.search_doctors(d, seed["supervisor1"],
                                   office_id=seed["office1"],
                                   doctor_class="B")) == 1
    assert len(docs.search_doctors(d, seed["supervisor1"],
                                   office_id=seed["office1"],
                                   product_id=p1)) == 1
    assert len(docs.search_doctors(d, seed["supervisor1"],
                                   office_id=seed["office1"],
                                   assigned_rep_id=seed["rep1_o1"])) == 1
    assert len(docs.search_doctors(d, seed["supervisor1"],
                                   office_id=seed["office1"],
                                   name="سارة")) == 1


def test_rep_cannot_manage_doctors(seed):
    d = seed["data_dir"]
    with pytest.raises(PermissionDenied):
        docs.create_doctor(d, seed["rep1_o1"], office_id=seed["office1"],
                           name="د. س")


def test_doctors_isolated_per_office(seed, full_doc):
    d = seed["data_dir"]
    did, _, _ = full_doc
    with pytest.raises(NotFound):
        docs.get_doctor(d, seed["supervisor2"], office_id=seed["office2"],
                        doctor_id=did)
    assert docs.search_doctors(d, seed["supervisor2"],
                               office_id=seed["office2"]) == []
