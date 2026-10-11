"""Product catalog per office: units + products.

Writes need product.manage (supervisor+). Reads need any active office
membership (reps will pick products when recording visits).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event


def _require_member(isess: Session, actor: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=actor, office_id=office_id, status="active").first())
    if m is None:
        # global owner (no office-scoped membership) may read everything,
        # mirroring require_permission's owner-grants-everything rule
        owner = (isess.query(im.Membership)
                 .filter_by(user_id=actor, role="owner",
                            status="active").first())
        if owner is None:
            raise PermissionDenied("no active membership in this office")


def create_unit(data_dir: Path, actor: int, *, office_id: int, name: str,
                description: str = "") -> dict:
    if not name.strip():
        raise ValidationError("unit name is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "product.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        if osess.query(om.ProductUnit).filter_by(name=name.strip()).first():
            raise AlreadyExists("unit already exists")
        u = om.ProductUnit(name=name.strip(), description=description)
        osess.add(u)
        osess.flush()
        return {"id": u.id, "name": u.name}


def create_product(data_dir: Path, actor: int, *, office_id: int,
                   trade_name: str, scientific_name: str = "",
                   manufacturer: str = "", dosage_form: str = "",
                   strength: str = "", unit_id: int | None = None) -> dict:
    if not trade_name.strip():
        raise ValidationError("trade name is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "product.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        if unit_id is not None and osess.get(om.ProductUnit, unit_id) is None:
            raise NotFound("unit not found")
        p = om.Product(trade_name=trade_name.strip(),
                       scientific_name=scientific_name, manufacturer=manufacturer,
                       dosage_form=dosage_form, strength=strength,
                       unit_id=unit_id, created_by=actor)
        osess.add(p)
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="PRODUCT_ADDED",
                         entity="product", entity_id=p.id)
        return {"id": p.id, "trade_name": p.trade_name}


def set_product_status(data_dir: Path, actor: int, *, office_id: int,
                       product_id: int, active: bool) -> dict:
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "product.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        p = osess.get(om.Product, product_id)
        if p is None:
            raise NotFound("product not found")
        p.status = "active" if active else "inactive"
        p.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor,
                         event_type="PRODUCT_STATUS_CHANGED",
                         entity="product", entity_id=p.id)
        return {"id": p.id, "status": p.status}


def list_products(data_dir: Path, actor: int, *, office_id: int,
                  active_only: bool = True) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        _require_member(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.Product).order_by(om.Product.trade_name)
        if active_only:
            q = q.filter_by(status="active")
        return [{"id": p.id, "trade_name": p.trade_name,
                 "scientific_name": p.scientific_name,
                 "manufacturer": p.manufacturer, "status": p.status}
                for p in q.all()]
