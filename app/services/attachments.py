"""Attachments: real file storage, not just metadata.

- Files live under ``data/<office_id>/attachments/`` with uuid-based stored
  names (uuid + original extension). User-supplied filenames are NEVER used
  as paths — this makes path traversal structurally impossible, and the
  final resolved path is additionally verified to stay inside the dir.
- 10MB per-file cap; allowlist of extensions/MIME (images, PDF, Excel, CSV).
- Soft delete: the row is flagged deleted + an audit event is logged; the
  file itself is kept on disk for forensics.
- Permissions mirror the owning entity: visits follow visit.record /
  visit.correct; doctors require directory.manage.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event

MAX_SIZE_BYTES = 10 * 1024 * 1024

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": ("application/vnd.openxmlformats-officedocument"
              ".spreadsheetml.sheet"),
    ".csv": "text/csv",
}

# entity_type -> (entity model, label) for existence checks
_ENTITY_MODELS = {
    "visit": om.ActualVisit,
    "doctor": om.Doctor,
}


def attachments_dir(data_dir: Path, office_id: int) -> Path:
    d = data_dir / str(office_id) / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_member(isess: Session, actor: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=actor, office_id=office_id, status="active")
         .first())
    if m is None:
        raise PermissionDenied("no active membership in this office")


def _require_entity_access(isess: Session, osess: Session, actor: int,
                           office_id: int, entity_type: str,
                           entity_id: int) -> None:
    """Permission for upload/download/delete on one entity's attachments."""
    if entity_type == "visit":
        v = osess.get(om.ActualVisit, entity_id)
        if v is None:
            raise NotFound("visit not found")
        try:
            require_permission(isess, actor, "visit.correct",
                               office_id=office_id)
            return
        except PermissionDenied:
            pass
        require_permission(isess, actor, "visit.record",
                           office_id=office_id)
        if actor != v.representative_id:
            raise PermissionDenied("can only attach to own visits")
    elif entity_type == "doctor":
        if osess.get(om.Doctor, entity_id) is None:
            raise NotFound("doctor not found")
        require_permission(isess, actor, "directory.manage",
                           office_id=office_id)
    else:
        raise ValidationError(f"unsupported entity_type: {entity_type}")


def _clean_display_name(filename: str) -> str:
    # basename kills any directory component; strip control chars
    name = os.path.basename(filename or "").strip()
    name = "".join(c for c in name if c.isprintable())
    if not name:
        raise ValidationError("filename is required")
    return name[:160]


def _stored_name(display_name: str) -> tuple[str, str]:
    ext = os.path.splitext(display_name)[1].lower()
    if ext not in _MIME_BY_EXT:
        raise ValidationError(
            f"file type not allowed: {ext or '(none)'} "
            f"(allowed: {', '.join(sorted(_MIME_BY_EXT))})")
    return f"{uuid.uuid4().hex}{ext}", _MIME_BY_EXT[ext]


def _safe_path(adir: Path, stored: str) -> Path:
    # stored names are uuid-generated, but verify containment anyway
    p = (adir / stored).resolve()
    if not p.is_relative_to(adir.resolve()):
        raise ValidationError("unsafe storage path")
    return p


def upload_attachment(data_dir: Path, actor: int, *, office_id: int,
                      entity_type: str, entity_id: int, filename: str,
                      content_bytes: bytes) -> dict:
    if entity_type not in _ENTITY_MODELS:
        raise ValidationError(f"unsupported entity_type: {entity_type}")
    if not isinstance(content_bytes, (bytes, bytearray)) or not content_bytes:
        raise ValidationError("file content is required")
    if len(content_bytes) > MAX_SIZE_BYTES:
        raise ValidationError(
            f"file too large: {len(content_bytes)} bytes "
            f"(max {MAX_SIZE_BYTES})")
    display = _clean_display_name(filename)
    stored, mime = _stored_name(display)
    with dbm.identity_session(data_dir) as s:
        _require_member(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        with dbm.identity_session(data_dir) as s:
            _require_entity_access(s, osess, actor, office_id,
                                   entity_type, entity_id)
        adir = attachments_dir(data_dir, office_id)
        path = _safe_path(adir, stored)
        path.write_bytes(bytes(content_bytes))
        os.chmod(path, 0o600)
        a = om.Attachment(owner_type=entity_type, owner_id=int(entity_id),
                          file_name=display, stored_path=str(path),
                          mime=mime, size_bytes=len(content_bytes),
                          uploaded_by=actor)
        osess.add(a)
        osess.flush()
        log_office_event(osess, actor_user_id=actor,
                         event_type="ATTACHMENT_UPLOADED",
                         entity="attachment", entity_id=a.id,
                         reason=f"{entity_type}:{entity_id} {display}")
        return _to_dict(a)


def _get_visible(osess: Session, attachment_id: int) -> om.Attachment:
    a = osess.get(om.Attachment, attachment_id)
    if a is None or a.deleted:
        raise NotFound("attachment not found")
    return a


def download_attachment(data_dir: Path, actor: int, *, office_id: int,
                        attachment_id: int) -> dict:
    with dbm.identity_session(data_dir) as s:
        _require_member(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        a = _get_visible(osess, attachment_id)
        with dbm.identity_session(data_dir) as s:
            _require_entity_access(s, osess, actor, office_id,
                                   a.owner_type, a.owner_id)
        adir = attachments_dir(data_dir, office_id)
        path = _safe_path(adir, Path(a.stored_path).name)
        if not path.exists():
            raise NotFound("attachment file missing from storage")
        return {"file_name": a.file_name, "mime": a.mime,
                "size_bytes": a.size_bytes,
                "content": path.read_bytes()}


def delete_attachment(data_dir: Path, actor: int, *, office_id: int,
                      attachment_id: int) -> dict:
    """Soft delete: flag the row, keep the file, log an audit event."""
    with dbm.identity_session(data_dir) as s:
        _require_member(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        a = _get_visible(osess, attachment_id)
        with dbm.identity_session(data_dir) as s:
            # The uploader may delete their own; otherwise supervisor+ or a
            # directory manager is required.
            if actor != a.uploaded_by:
                try:
                    require_permission(s, actor, "visit.correct",
                                       office_id=office_id)
                except PermissionDenied:
                    require_permission(s, actor, "directory.manage",
                                       office_id=office_id)
        a.deleted = True
        a.deleted_at = utcnow()
        log_office_event(osess, actor_user_id=actor,
                         event_type="ATTACHMENT_DELETED",
                         entity="attachment", entity_id=a.id,
                         reason=a.file_name)
        return {"id": a.id, "deleted": True}


def list_attachments(data_dir: Path, actor: int, *, office_id: int,
                     entity_type: str, entity_id: int) -> list[dict]:
    if entity_type not in _ENTITY_MODELS:
        raise ValidationError(f"unsupported entity_type: {entity_type}")
    with dbm.identity_session(data_dir) as s:
        _require_member(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        with dbm.identity_session(data_dir) as s:
            _require_entity_access(s, osess, actor, office_id,
                                   entity_type, entity_id)
        rows = (osess.query(om.Attachment)
                .filter_by(owner_type=entity_type, owner_id=int(entity_id),
                           deleted=False)
                .order_by(om.Attachment.id).all())
        return [_to_dict(a) for a in rows]


def _to_dict(a: om.Attachment) -> dict:
    return {"id": a.id, "owner_type": a.owner_type, "owner_id": a.owner_id,
            "file_name": a.file_name, "mime": a.mime,
            "size_bytes": a.size_bytes, "uploaded_by": a.uploaded_by,
            "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None}
