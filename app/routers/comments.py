# @file    comments.py

import logging
from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import Comment, CaptionVersion, Signoff, User
from app.schemas.schemas import CommentOut, CommentCreate
from app.services.notification_service import (
    NOTIF_COMMENT_MENTION,
    extract_mentioned_user_ids,
    notify_users,
)

_log = logging.getLogger(__name__)

router = APIRouter()


def _list_comments(db: Session, entity_id: str, caption: str) -> list[Comment]:
    """Return root comments for a caption — empty list when none exist."""
    return (
        db.query(Comment)
        .filter(
            Comment.entity_id == entity_id,
            Comment.caption   == caption,
            Comment.parent_id == None,
        )
        .order_by(Comment.created_at)
        .all()
    )


@router.get(
    "/{entity_id}/comments",
    response_model=List[CommentOut],
    dependencies=[Depends(require_entity_access())],
    summary="List top-level comments for a caption (query param)",
    description=(
        "Preferred route for captions containing `/`, `&`, or other "
        "characters that break path segments. Pass "
        "`?caption=Fixed%20Assets%20%2F%20PP%26E`."
    ),
)
def get_comments_by_query(
    entity_id: str,
    caption: str = Query(..., min_length=1, description="Flux caption label"),
    db: Session = Depends(get_db),
):
    return _list_comments(db, entity_id, caption)


@router.get(
    "/{entity_id}/comments/{caption}",
    response_model=List[CommentOut],
    dependencies=[Depends(require_entity_access())],
    summary="List top-level comments for a caption (legacy path)",
    description=(
        "Legacy path-segment route kept for backward compatibility. "
        "Captions containing `/` must use the query-param route above."
    ),
)
def get_comments(entity_id: str, caption: str, db: Session = Depends(get_db)):
    return _list_comments(db, entity_id, caption)


def _create_comment(
    db: Session,
    *,
    entity_id: str,
    caption: str,
    body: CommentCreate,
    request: Request,
    user: User,
) -> Comment:
    comment = Comment(
        entity_id=entity_id, caption=caption,
        user_id=user.id, text=body.text, parent_id=body.parent_id,
    )
    db.add(comment)

    cv = (db.query(CaptionVersion)
            .filter(CaptionVersion.entity_id == entity_id, CaptionVersion.caption == caption)
            .first())
    if cv:
        cv.version += 1
    else:
        cv = CaptionVersion(entity_id=entity_id, caption=caption, version=1)
        db.add(cv)

    for s in db.query(Signoff).filter(
        Signoff.entity_id == entity_id, Signoff.caption == caption,
    ).all():
        s.is_stale = True

    log_change(db, actor=user, action="comment.create",
               table_name="comments", row_pk=f"{entity_id}/{caption}",
               after={"text": body.text, "parent_id": body.parent_id}, request=request)

    # @mention notifications
    try:
        mentioned_ids = extract_mentioned_user_ids(db, body.text)
        if mentioned_ids:
            notify_users(
                db,
                mentioned_ids,
                notif_type=NOTIF_COMMENT_MENTION,
                title="You were mentioned",
                message=f"{user.name} mentioned you in {entity_id} / {caption}",
                actor_id=user.id,
                entity_id=entity_id,
                link_url=f"/supervisor/flux-review?entity={entity_id}&caption={caption}",
            )
    except Exception:
        _log.warning("Mention notification failed (non-blocking)", exc_info=False)

    db.commit()
    db.refresh(comment)
    return comment


@router.post(
    "/{entity_id}/comments",
    response_model=CommentOut,
    status_code=201,
    dependencies=[Depends(require_entity_access())],
    summary="Add comment (query param caption)",
    description=(
        "Preferred route when the caption contains `/`, `&`, spaces, or "
        "brackets. Pass `?caption=` with the full caption string."
    ),
)
def add_comment_by_query(
    entity_id: str,
    body: CommentCreate,
    request: Request,
    caption: str = Query(..., min_length=1, description="Flux caption label"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _create_comment(
        db, entity_id=entity_id, caption=caption, body=body,
        request=request, user=user,
    )


@router.post(
    "/{entity_id}/comments/{caption}",
    response_model=CommentOut,
    status_code=201,
    dependencies=[Depends(require_entity_access())],
    summary="Add comment (legacy path caption)",
    description="Legacy path-segment route kept for backward compatibility.",
)
def add_comment(
    entity_id: str, caption: str, body: CommentCreate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    return _create_comment(
        db, entity_id=entity_id, caption=caption, body=body,
        request=request, user=user,
    )


@router.patch(
    "/{entity_id}/comments/{comment_id}/resolve",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Resolve a comment",
    description="Supervisor, director, or admin marks comment resolved (required before entity approve).",
    responses={404: {"description": "Comment not found"}},
)
def resolve_comment(
    entity_id: str, comment_id: int, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    c = db.query(Comment).filter(Comment.id == comment_id, Comment.entity_id == entity_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Comment not found")
    c.is_resolved = True
    c.resolved_by = user.id
    c.resolved_at = datetime.utcnow()
    log_change(db, actor=user, action="comment.resolve",
               table_name="comments", row_pk=str(comment_id), request=request)
    db.commit()
    return {"id": comment_id, "resolved": True}
