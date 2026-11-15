# @file    services/notification_service.py
#
# Centralised notification service for FinFlux in-app notifications.
# Email support is designed-in but not activated — wire it later via
# the `_send_email_channel()` stub at the bottom.
#
# RULES:
#  - Notification creation never raises to callers (wrap externally in try/except).
#  - Self-notification is skipped by default (skip_self=True).
#  - Duplicate check prevents double-fire within a 60-second window.
#  - No secrets / plaintext credentials stored in metadata_json.

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import Notification, User, UserEntityAccess

_log = logging.getLogger(__name__)

# ── Notification type constants ────────────────────────────────────────
NOTIF_FLUX_SUBMITTED       = "flux_submitted"
NOTIF_FLUX_REJECTED        = "flux_rejected"
NOTIF_FLUX_APPROVED        = "flux_approved"
NOTIF_DIRECTOR_REQUIRED    = "director_approval_required"
NOTIF_DIRECTOR_APPROVED    = "director_approved"
NOTIF_DIRECTOR_REJECTED    = "director_rejected"
NOTIF_COMMENT_MENTION      = "comment_mention"
NOTIF_TASK_ASSIGNED        = "task_assigned"
NOTIF_TASK_REASSIGNED      = "task_reassigned"
NOTIF_DUE_DATE_REMINDER    = "due_date_reminder"
NOTIF_BC_SYNC_FAILED       = "bc_sync_failed"
NOTIF_AI_COMPLETED         = "ai_analysis_completed"
NOTIF_AI_FAILED            = "ai_analysis_failed"

_MENTION_RE = re.compile(r'@(\w+ \w+)')
_DEDUP_WINDOW_SECONDS = 60


# ── Internal helpers ───────────────────────────────────────────────────

def _dedup_exists(
    db: Session,
    recipient_id: int,
    notif_type: str,
    entity_id: Optional[str],
) -> bool:
    cutoff = datetime.utcnow() - timedelta(seconds=_DEDUP_WINDOW_SECONDS)
    q = db.query(Notification).filter(
        Notification.recipient_user_id == recipient_id,
        Notification.type == notif_type,
        Notification.created_at >= cutoff,
    )
    if entity_id:
        q = q.filter(Notification.entity_id == entity_id)
    return q.first() is not None


# ── Core creation ──────────────────────────────────────────────────────

def create_notification(
    db: Session,
    *,
    recipient_id: int,
    notif_type: str,
    title: str,
    message: str,
    actor_id: Optional[int] = None,
    entity_id: Optional[str] = None,
    link_url: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    skip_self: bool = True,
) -> Optional[Notification]:
    """
    Create a single in-app notification row.

    Returns None without raising if:
      - skip_self=True and recipient == actor
      - a duplicate notification exists within the dedup window
    """
    if skip_self and actor_id is not None and recipient_id == actor_id:
        return None
    if _dedup_exists(db, recipient_id, notif_type, entity_id):
        return None

    notif = Notification(
        recipient_user_id=recipient_id,
        actor_user_id=actor_id,
        entity_id=entity_id,
        type=notif_type,
        title=title,
        message=message,
        link_url=link_url,
        metadata_json=metadata,
    )
    db.add(notif)
    db.flush()
    return notif


def notify_users(
    db: Session,
    user_ids: list[int],
    *,
    notif_type: str,
    title: str,
    message: str,
    actor_id: Optional[int] = None,
    entity_id: Optional[str] = None,
    link_url: Optional[str] = None,
    skip_self: bool = True,
) -> list[Notification]:
    """Create notifications for multiple recipients."""
    results: list[Notification] = []
    for uid in user_ids:
        n = create_notification(
            db,
            recipient_id=uid,
            notif_type=notif_type,
            title=title,
            message=message,
            actor_id=actor_id,
            entity_id=entity_id,
            link_url=link_url,
            skip_self=skip_self,
        )
        if n:
            results.append(n)
    return results


# ── Role-targeted helpers ──────────────────────────────────────────────

def notify_supervisor_for_entity(
    db: Session,
    entity: Any,
    *,
    notif_type: str,
    title: str,
    message: str,
    actor_id: Optional[int] = None,
    link_url: Optional[str] = None,
) -> Optional[Notification]:
    return create_notification(
        db,
        recipient_id=entity.supervisor_id,
        notif_type=notif_type,
        title=title,
        message=message,
        actor_id=actor_id,
        entity_id=entity.id,
        link_url=link_url,
    )


def notify_director_for_entity(
    db: Session,
    entity: Any,
    *,
    notif_type: str,
    title: str,
    message: str,
    actor_id: Optional[int] = None,
    link_url: Optional[str] = None,
) -> Optional[Notification]:
    if not entity.director_id:
        return None
    return create_notification(
        db,
        recipient_id=entity.director_id,
        notif_type=notif_type,
        title=title,
        message=message,
        actor_id=actor_id,
        entity_id=entity.id,
        link_url=link_url,
    )


def notify_admins(
    db: Session,
    *,
    notif_type: str,
    title: str,
    message: str,
    actor_id: Optional[int] = None,
    entity_id: Optional[str] = None,
    link_url: Optional[str] = None,
    skip_self: bool = False,
) -> list[Notification]:
    """Notify all active admin users. Defaults skip_self=False for operational alerts."""
    admin_ids = [
        r[0]
        for r in db.query(User.id)
        .filter(User.role == "admin", User.status == "active")
        .all()
    ]
    return notify_users(
        db,
        admin_ids,
        notif_type=notif_type,
        title=title,
        message=message,
        actor_id=actor_id,
        entity_id=entity_id,
        link_url=link_url,
        skip_self=skip_self,
    )


def notify_entity_analysts(
    db: Session,
    entity_id: str,
    *,
    notif_type: str,
    title: str,
    message: str,
    actor_id: Optional[int] = None,
    link_url: Optional[str] = None,
) -> list[Notification]:
    """Notify all analysts assigned to this entity via UserEntityAccess."""
    analyst_ids = [
        r[0]
        for r in db.query(UserEntityAccess.user_id)
        .join(User, User.id == UserEntityAccess.user_id)
        .filter(
            UserEntityAccess.entity_id == entity_id,
            User.role == "analyst",
            User.status == "active",
        )
        .all()
    ]
    return notify_users(
        db,
        analyst_ids,
        notif_type=notif_type,
        title=title,
        message=message,
        actor_id=actor_id,
        entity_id=entity_id,
        link_url=link_url,
    )


# ── @mention extraction ────────────────────────────────────────────────

def extract_mentioned_user_ids(db: Session, text: str) -> list[int]:
    """
    Extract user IDs from @mention patterns like '@Jane Smith' in comment text.
    Matches the frontend MText regex: /@\\w+ \\w+/g
    """
    names = _MENTION_RE.findall(text)
    if not names:
        return []
    users = (
        db.query(User)
        .filter(
            func.concat(User.first_name, " ", User.last_name).in_(names),
            User.status == "active",
        )
        .all()
    )
    return [u.id for u in users]


# ── Read / query operations (used by the notifications router) ─────────

def get_user_notifications(
    db: Session,
    user_id: int,
    *,
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[Notification]:
    q = db.query(Notification).filter(Notification.recipient_user_id == user_id)
    if unread_only:
        q = q.filter(Notification.is_read.is_(False))
    return q.order_by(Notification.created_at.desc()).offset(offset).limit(limit).all()


def get_unread_count(db: Session, user_id: int) -> int:
    return (
        db.query(Notification)
        .filter(
            Notification.recipient_user_id == user_id,
            Notification.is_read.is_(False),
        )
        .count()
    )


def mark_notification_read(db: Session, notification_id: int, user_id: int) -> bool:
    """Mark one notification read. Returns False if not found / not owned."""
    n = (
        db.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.recipient_user_id == user_id,
        )
        .first()
    )
    if not n:
        return False
    n.is_read = True
    n.read_at = datetime.utcnow()
    db.commit()
    return True


def mark_all_notifications_read(db: Session, user_id: int) -> int:
    """Mark all unread notifications for a user as read. Returns count updated."""
    count = (
        db.query(Notification)
        .filter(
            Notification.recipient_user_id == user_id,
            Notification.is_read.is_(False),
        )
        .update({"is_read": True, "read_at": datetime.utcnow()})
    )
    db.commit()
    return count


# ── Due-date reminder helper (for future scheduler) ────────────────────

def create_due_date_reminder(
    db: Session,
    *,
    recipient_ids: list[int],
    entity_id: str,
    entity_name: str,
    due_label: str,
    link_url: Optional[str] = None,
) -> list[Notification]:
    """
    Helper for a future scheduler to push due-date reminders.
    Call from a cron/Celery task — not wired to a live scheduler yet.
    """
    return notify_users(
        db,
        recipient_ids,
        notif_type=NOTIF_DUE_DATE_REMINDER,
        title="Due Date Reminder",
        message=f"{entity_name} — {due_label} is approaching.",
        entity_id=entity_id,
        link_url=link_url,
        skip_self=False,
    )


# ── Email channel stub (Phase 2) ───────────────────────────────────────

def _send_email_channel(recipient_email: str, title: str, message: str) -> None:
    """
    Placeholder for Phase 2 email delivery.
    Wire to app.services.email.send_email() when ready.
    Currently a no-op.
    """
