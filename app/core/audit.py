# @file    audit.py
#
# Tiny audit-log writer. Used by admin endpoints + workflow status changes.

from __future__ import annotations
import json
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.models import AuditLog, User


def _safe_json(v: Any) -> dict | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    try:
        return json.loads(json.dumps(v, default=str))
    except Exception:
        return {"_repr": str(v)}


def log_change(
    db: Session,
    *,
    actor: User,
    action: str,
    table_name: str,
    row_pk: str | int | None = None,
    before: Any = None,
    after: Any = None,
    request: Request | None = None,
) -> AuditLog:
    """Write an audit row. Caller is responsible for the surrounding commit."""
    ip = None
    if request is not None:
        ip = request.client.host if request.client else None

    row = AuditLog(
        actor_user_id=actor.id,
        action=action,
        table_name=table_name,
        row_pk=str(row_pk) if row_pk is not None else None,
        before_json=_safe_json(before),
        after_json=_safe_json(after),
        ip_address=ip,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row
