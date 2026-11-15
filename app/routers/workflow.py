# @file    workflow.py
#
# Higher-level entity-status transitions:
#   POST /api/entities/{id}/submit          (analyst -> under_review)
#   POST /api/entities/{id}/approve         (supervisor or director final)
#   POST /api/entities/{id}/reject          (supervisor)
#   POST /api/entities/{id}/send-for-approval  (supervisor -> pending_director)
#
# Centralises the unresolved-comments-blocks-approval rule.

import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import Entity, Comment, ChatMessage, User
from app.schemas.schemas import WorkflowAction
from app.services.notification_service import (
    NOTIF_DIRECTOR_APPROVED,
    NOTIF_DIRECTOR_REJECTED,
    NOTIF_DIRECTOR_REQUIRED,
    NOTIF_FLUX_APPROVED,
    NOTIF_FLUX_REJECTED,
    NOTIF_FLUX_SUBMITTED,
    notify_director_for_entity,
    notify_entity_analysts,
    notify_supervisor_for_entity,
)

_log = logging.getLogger(__name__)

router = APIRouter()


def _unresolved_count(db: Session, entity_id: str) -> int:
    return (
        db.query(Comment)
        .filter(Comment.entity_id == entity_id, Comment.is_resolved == False)
        .count()
    )


def _safe_notify(fn, *args, **kwargs) -> None:
    """Call a notification helper without letting exceptions surface."""
    try:
        fn(*args, **kwargs)
    except Exception:
        _log.warning("Notification creation failed (non-blocking)", exc_info=False)


@router.post(
    "/{entity_id}/submit",
    dependencies=[Depends(require_entity_access())],
    summary="Submit entity for review",
    description="Analyst (or admin) sets status to **under_review**.",
    responses={403: {"description": "Only analysts may submit"}, 404: {"description": "Entity not found"}},
)
def submit_entity(
    entity_id: str, body: WorkflowAction, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if user.role not in ("analyst", "admin"):
        raise HTTPException(status_code=403, detail="Only analysts can submit")
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    before = {"status": e.status}
    e.status = "under_review"
    log_change(db, actor=user, action="workflow.submit",
               table_name="entities", row_pk=entity_id,
               before=before, after={"status": e.status, "note": body.note}, request=request)
    _safe_notify(
        notify_supervisor_for_entity, db, e,
        notif_type=NOTIF_FLUX_SUBMITTED,
        title="Flux Analysis Submitted",
        message=f"{user.name} submitted flux analysis for {e.name}",
        actor_id=user.id,
        link_url=f"/supervisor/flux-review?entity={entity_id}",
    )
    db.commit()
    return {"id": entity_id, "status": e.status}


@router.post(
    "/{entity_id}/approve",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Approve entity",
    description="Sets **approved**; returns **409** if any comment on the entity is unresolved.",
    responses={409: {"description": "Unresolved comments block approval"}},
)
def approve_entity(
    entity_id: str, body: WorkflowAction, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    unresolved = _unresolved_count(db, entity_id)
    if unresolved > 0:
        raise HTTPException(status_code=409, detail=f"Cannot approve: {unresolved} unresolved comment(s)")
    before = {"status": e.status}
    e.status = "approved"
    log_change(db, actor=user, action="workflow.approve",
               table_name="entities", row_pk=entity_id,
               before=before, after={"status": e.status, "note": body.note}, request=request)

    if user.role in ("director", "admin") and e.director_id:
        # Director final approval — notify supervisor + all analysts
        _safe_notify(
            notify_supervisor_for_entity, db, e,
            notif_type=NOTIF_DIRECTOR_APPROVED,
            title="Director Approved",
            message=f"{user.name} (Director) approved {e.name}.",
            actor_id=user.id,
            link_url=f"/supervisor/flux-review?entity={entity_id}",
        )
        _safe_notify(
            notify_entity_analysts, db, entity_id,
            notif_type=NOTIF_DIRECTOR_APPROVED,
            title="Director Approved",
            message=f"{e.name} received final director approval.",
            actor_id=user.id,
            link_url=f"/analyst/flux-review?entity={entity_id}",
        )
    elif e.approval_level == "director" and e.director_id:
        # Supervisor approved but director is required — notify director
        _safe_notify(
            notify_director_for_entity, db, e,
            notif_type=NOTIF_DIRECTOR_REQUIRED,
            title="Director Approval Required",
            message=f"{user.name} approved {e.name}. Director approval required.",
            actor_id=user.id,
            link_url="/director/queue",
        )
    else:
        # Supervisor final approval — notify analysts
        _safe_notify(
            notify_entity_analysts, db, entity_id,
            notif_type=NOTIF_FLUX_APPROVED,
            title="Flux Analysis Approved",
            message=f"{e.name} approved by supervisor.",
            actor_id=user.id,
            link_url=f"/analyst/flux-review?entity={entity_id}",
        )

    db.commit()
    return {"id": entity_id, "status": e.status}


@router.post(
    "/{entity_id}/reject",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Reject entity",
    description="Sets **rejected**; optional note posts a flagged FinChat line.",
    responses={404: {"description": "Entity not found"}},
)
def reject_entity(
    entity_id: str, body: WorkflowAction, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    before = {"status": e.status}
    e.status = "rejected"
    if body.note:
        db.add(ChatMessage(entity_id=entity_id, user_id=user.id,
                           text=f"[REJECTED] {body.note}", is_flagged=True))
    log_change(db, actor=user, action="workflow.reject",
               table_name="entities", row_pk=entity_id,
               before=before, after={"status": e.status, "note": body.note}, request=request)

    if user.role in ("director", "admin") and e.director_id:
        # Director rejection
        _safe_notify(
            notify_supervisor_for_entity, db, e,
            notif_type=NOTIF_DIRECTOR_REJECTED,
            title="Director Rejected",
            message=f"{user.name} (Director) rejected {e.name}.",
            actor_id=user.id,
            link_url=f"/supervisor/flux-review?entity={entity_id}",
        )
        _safe_notify(
            notify_entity_analysts, db, entity_id,
            notif_type=NOTIF_DIRECTOR_REJECTED,
            title="Director Rejected",
            message=f"{e.name} was rejected by the director.",
            actor_id=user.id,
            link_url=f"/analyst/flux-review?entity={entity_id}",
        )
    else:
        # Supervisor rejection
        _safe_notify(
            notify_entity_analysts, db, entity_id,
            notif_type=NOTIF_FLUX_REJECTED,
            title="Flux Analysis Rejected",
            message=f"{user.name} rejected flux analysis for {e.name}.",
            actor_id=user.id,
            link_url=f"/analyst/flux-review?entity={entity_id}",
        )

    db.commit()
    return {"id": entity_id, "status": e.status}


@router.post(
    "/{entity_id}/send-for-approval",
    dependencies=[Depends(require_role("supervisor", "admin"))],
    summary="Send to director queue",
    description="Requires entity `approval_level=director` and `director_id`; **409** if unresolved comments.",
    responses={
        400: {"description": "No director configured on entity"},
        409: {"description": "Unresolved comments"},
    },
)
def send_for_director_approval(
    entity_id: str, body: WorkflowAction, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    if e.approval_level != "director" or not e.director_id:
        raise HTTPException(status_code=400, detail="Entity has no director approver configured")
    unresolved = _unresolved_count(db, entity_id)
    if unresolved > 0:
        raise HTTPException(status_code=409, detail=f"Cannot send: {unresolved} unresolved comment(s)")
    before = {"status": e.status}
    e.status = "pending_director"
    log_change(db, actor=user, action="workflow.send_for_director",
               table_name="entities", row_pk=entity_id,
               before=before, after={"status": e.status, "note": body.note}, request=request)
    _safe_notify(
        notify_director_for_entity, db, e,
        notif_type=NOTIF_DIRECTOR_REQUIRED,
        title="Director Approval Required",
        message=f"{user.name} approved {e.name}. Director approval required.",
        actor_id=user.id,
        link_url="/director/queue",
    )
    db.commit()
    return {"id": entity_id, "status": e.status}


@router.get(
    "/director/queue",
    dependencies=[Depends(require_role("director", "admin"))],
    summary="Director final-approval queue",
    description="Full URL path: **`/api/entities/director/queue`**. Directors see only entities where they are `director_id` and status is **pending_director**; admins see all pending.",
)
def director_queue(
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    q = db.query(Entity).filter(Entity.status == "pending_director")
    if user.role == "director":
        q = q.filter(Entity.director_id == user.id)
    rows = q.all()
    return [{"id": e.id, "name": e.name, "region": e.region, "status": e.status,
             "supervisor_id": e.supervisor_id, "period_from": e.period_from,
             "period_to": e.period_to, "rev_due_date": str(e.rev_due_date) if e.rev_due_date else None}
            for e in rows]
