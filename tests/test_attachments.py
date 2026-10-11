"""Attachments: real file storage under data/<office_id>/attachments/.

All tests run on the isolated seed fixture dir — never real data.
"""
import os
from pathlib import Path

import pytest

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import attachments as att
from app.services import doctors as doctors_svc
from app.services import visits as visits_svc

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


def _doctor_id(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    out = doctors_svc.create_doctor(d, seed["supervisor1"], office_id=o1,
                                    name="د. اختبار مرفقات")
    return out["id"]


def _visit_id(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    did = _doctor_id(seed)
    r = visits_svc.record_actual_visit(
        d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
        target_type="doctor", target_id=did, confirm=True)
    return r["visit_id"]


def test_upload_download_roundtrip(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    meta = att.upload_attachment(
        d, seed["rep1_o1"], office_id=o1, entity_type="visit",
        entity_id=vid, filename="report.png", content_bytes=PNG)
    assert meta["mime"] == "image/png"
    assert meta["size_bytes"] == len(PNG)
    assert meta["file_name"] == "report.png"
    got = att.download_attachment(d, seed["rep1_o1"], office_id=o1,
                                  attachment_id=meta["id"])
    assert got["content"] == PNG
    assert got["file_name"] == "report.png"
    # file actually on disk under data/<office_id>/attachments/
    adir = d / str(o1) / "attachments"
    assert adir.is_dir()
    assert len(list(adir.iterdir())) == 1


def test_attachment_visible_in_visit_data(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    meta = att.upload_attachment(
        d, seed["rep1_o1"], office_id=o1, entity_type="visit",
        entity_id=vid, filename="x.pdf", content_bytes=b"%PDF-1.4 fake")
    detail = visits_svc.get_actual_visit(d, seed["rep1_o1"], office_id=o1,
                                         visit_id=vid)
    assert [a["id"] for a in detail["attachments"]] == [meta["id"]]


def test_attachment_visible_in_doctor_profile(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    did = _doctor_id(seed)
    meta = att.upload_attachment(
        d, seed["supervisor1"], office_id=o1, entity_type="doctor",
        entity_id=did, filename="card.jpg", content_bytes=PNG)
    profile = doctors_svc.get_doctor(d, seed["supervisor1"], office_id=o1,
                                     doctor_id=did)
    assert [a["id"] for a in profile["attachments"]] == [meta["id"]]


def test_reject_oversize_file(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    big = b"z" * (att.MAX_SIZE_BYTES + 1)
    with pytest.raises(ValidationError, match="too large"):
        att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                              entity_type="visit", entity_id=vid,
                              filename="big.png", content_bytes=big)


def test_reject_disallowed_extension(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    with pytest.raises(ValidationError, match="not allowed"):
        att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                              entity_type="visit", entity_id=vid,
                              filename="evil.exe", content_bytes=b"MZ")
    with pytest.raises(ValidationError, match="not allowed"):
        att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                              entity_type="visit", entity_id=vid,
                              filename="noext", content_bytes=b"data")


def test_path_traversal_filename_is_neutralized(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    meta = att.upload_attachment(
        d, seed["rep1_o1"], office_id=o1, entity_type="visit",
        entity_id=vid, filename="../../etc/passwd.png",
        content_bytes=PNG)
    # display name keeps only the basename
    assert meta["file_name"] == "passwd.png"
    assert "/" not in meta["file_name"] and "\\" not in meta["file_name"]
    # nothing escaped the attachments dir
    assert not (d / "etc").exists()
    adir = (d / str(o1) / "attachments").resolve()
    stored_files = list(adir.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].resolve().is_relative_to(adir)


def test_permission_denied_other_office_rep(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    # rep_o2 belongs to office2, not office1
    with pytest.raises(PermissionDenied):
        att.upload_attachment(d, seed["rep_o2"], office_id=o1,
                              entity_type="visit", entity_id=vid,
                              filename="a.png", content_bytes=PNG)


def test_rep_cannot_attach_to_others_visit(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)  # belongs to rep1_o1
    with pytest.raises(PermissionDenied):
        att.upload_attachment(d, seed["rep2_o1"], office_id=o1,
                              entity_type="visit", entity_id=vid,
                              filename="a.png", content_bytes=PNG)
    # supervisor can
    meta = att.upload_attachment(d, seed["supervisor1"], office_id=o1,
                                 entity_type="visit", entity_id=vid,
                                 filename="a.png", content_bytes=PNG)
    assert meta["id"]


def test_soft_delete_hides_but_keeps_audit(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    meta = att.upload_attachment(
        d, seed["rep1_o1"], office_id=o1, entity_type="visit",
        entity_id=vid, filename="a.png", content_bytes=PNG)
    r = att.delete_attachment(d, seed["supervisor1"], office_id=o1,
                              attachment_id=meta["id"])
    assert r["deleted"] is True
    # download now fails
    with pytest.raises(NotFound):
        att.download_attachment(d, seed["rep1_o1"], office_id=o1,
                                attachment_id=meta["id"])
    # hidden from visit detail
    detail = visits_svc.get_actual_visit(d, seed["rep1_o1"], office_id=o1,
                                         visit_id=vid)
    assert detail["attachments"] == []
    # row still in DB flagged deleted + audit event logged
    with dbm.office_session(d, o1) as s:
        row = s.get(om.Attachment, meta["id"])
        assert row.deleted is True
        assert row.deleted_at is not None
        ev = (s.query(om.OfficeAuditEvent)
              .filter_by(event_type="ATTACHMENT_DELETED",
                         entity_id=meta["id"]).one())
        assert ev.result == "ok"


def test_list_attachments_filters_deleted(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    m1 = att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                               entity_type="visit", entity_id=vid,
                               filename="a.png", content_bytes=PNG)
    m2 = att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                               entity_type="visit", entity_id=vid,
                               filename="b.pdf", content_bytes=b"%PDF")
    att.delete_attachment(d, seed["rep1_o1"], office_id=o1,
                          attachment_id=m1["id"])
    rows = att.list_attachments(d, seed["rep1_o1"], office_id=o1,
                                entity_type="visit", entity_id=vid)
    assert [r["id"] for r in rows] == [m2["id"]]


def test_upload_rejects_empty_content(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    with pytest.raises(ValidationError):
        att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                              entity_type="visit", entity_id=vid,
                              filename="a.png", content_bytes=b"")


def test_stored_files_are_0600(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    vid = _visit_id(seed)
    att.upload_attachment(d, seed["rep1_o1"], office_id=o1,
                          entity_type="visit", entity_id=vid,
                          filename="a.png", content_bytes=PNG)
    adir = d / str(o1) / "attachments"
    for f in adir.iterdir():
        assert f.stat().st_mode & 0o777 == 0o600
