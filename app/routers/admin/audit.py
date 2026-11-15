# @file    admin/audit.py

from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import require_role
from app.db.session import get_db
from app.models.models import AuditLog
from app.schemas.schemas import AuditLogOut

router = APIRouter(dependencies=[Depends(require_role("admin"))])


@router.get(
    "",
    response_model=List[AuditLogOut],
    summary="Query audit logs",
    description="Filter by actor user id, action prefix, table name, time range; paginated with `limit`/`offset`.",
)
def list_audit_logs(
    actor: Optional[int]    = Query(None),
    action: Optional[str]   = Query(None),
    table:  Optional[str]   = Query(None),
    from_:  Optional[datetime] = Query(None, alias="from"),
    to:     Optional[datetime] = Query(None),
    limit:  int = Query(200, ge=1, le=1000),
    offset: int = Query(0,   ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog)
    if actor: q = q.filter(AuditLog.actor_user_id == actor)
    if action: q = q.filter(AuditLog.action.like(f"{action}%"))
    if table: q = q.filter(AuditLog.table_name == table)
    if from_: q = q.filter(AuditLog.created_at >= from_)
    if to:    q = q.filter(AuditLog.created_at <= to)
    return (q.order_by(AuditLog.created_at.desc())
              .offset(offset).limit(limit).all())
