"""Products & units catalog."""
import pytest

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.services import products as prod


def test_unit_and_product_flow(seed):
    d = seed["data_dir"]
    u = prod.create_unit(d, seed["supervisor1"], office_id=seed["office1"],
                         name="علبة")
    p = prod.create_product(
        d, seed["supervisor1"], office_id=seed["office1"],
        trade_name="دواء أ", scientific_name="مادة أ",
        manufacturer="شركة د", dosage_form="أقراص", strength="500mg",
        unit_id=u["id"])
    items = prod.list_products(d, seed["rep1_o1"], office_id=seed["office1"])
    assert len(items) == 1 and items[0]["trade_name"] == "دواء أ"


def test_duplicate_unit_rejected(seed):
    d = seed["data_dir"]
    prod.create_unit(d, seed["supervisor1"], office_id=seed["office1"], name="شريط")
    with pytest.raises(AlreadyExists):
        prod.create_unit(d, seed["supervisor1"], office_id=seed["office1"],
                         name="شريط")


def test_rep_cannot_manage_products(seed):
    d = seed["data_dir"]
    with pytest.raises(PermissionDenied):
        prod.create_unit(d, seed["rep1_o1"], office_id=seed["office1"], name="x")


def test_deactivate_hides_from_active_list(seed):
    d = seed["data_dir"]
    p = prod.create_product(d, seed["supervisor1"], office_id=seed["office1"],
                            trade_name="دواء ب")
    prod.set_product_status(d, seed["supervisor1"], office_id=seed["office1"],
                            product_id=p["id"], active=False)
    assert prod.list_products(d, seed["rep1_o1"],
                              office_id=seed["office1"]) == []
    assert len(prod.list_products(d, seed["rep1_o1"], office_id=seed["office1"],
                                  active_only=False)) == 1


def test_product_unknown_unit_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(NotFound):
        prod.create_product(d, seed["supervisor1"], office_id=seed["office1"],
                            trade_name="دواء ج", unit_id=9999)


def test_products_isolated_per_office(seed):
    d = seed["data_dir"]
    prod.create_product(d, seed["supervisor1"], office_id=seed["office1"],
                        trade_name="دواء أ")
    assert prod.list_products(d, seed["supervisor2"],
                              office_id=seed["office2"]) == []
