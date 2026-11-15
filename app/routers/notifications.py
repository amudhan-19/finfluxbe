# @file    routers/notifications.py
#
# In-app notification endpoints. Each user can only access their own notifications.
#
# GET  /api/notifications              — list (with optional unread_only / limit / offset)
# GET  /api/notifications/unread-count — lightweight badge count
# PATCH /api/notifications/{id}/read  — mark one read
# PATCH /api/notifications/read-all   — mark all read

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.models import User
from app.schemas.schemas import NotificationOut, UnreadCountOut
from app.services.notification_service import (
    get_unread_count,
    get_user_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

router = APIRouter()


@router.get(
    "",
    response_model=List[NotificationOut],
    summary="List my notifications",
    description="Returns the authenticated user's notifications, newest first. "
                "Filter with `unread_only=true`. Paginate with `limit` / `offset`.",
)
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[NotificationOut]:
    return get_user_notifications(
        db, user.id, unread_only=unread_only, limit=limit, offset=offset
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountOut,
    summary="Unread notification count",
    description="Lightweight endpoint — safe to poll every 30 seconds for the bell badge.",
)
def unread_count(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UnreadCountOut:
    return UnreadCountOut(count=get_unread_count(db, user.id))


@router.patch(
    "/{notification_id}/read",
    summary="Mark one notification as read",
    responses={404: {"description": "Notification not found or not yours"}},
)
def mark_one_read(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not mark_notification_read(db, notification_id, user.id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"id": notification_id, "is_read": True}


@router.patch(
    "/read-all",
    summary="Mark all notifications as read",
)
def mark_all_read(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    count = mark_all_notifications_read(db, user.id)
    return {"marked_read": count}
