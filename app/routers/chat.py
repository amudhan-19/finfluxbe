# @file    chat.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import ChatMessage, ChatReply, User
from app.schemas.schemas import ChatMessageOut, ChatMessageCreate, ChatReplyCreate

router = APIRouter()


@router.get(
    "/{entity_id}/chat",
    response_model=List[ChatMessageOut],
    dependencies=[Depends(require_entity_access())],
    summary="List FinChat messages",
    description="Messages with nested replies, ordered by `created_at`.",
)
def get_chat(entity_id: str, db: Session = Depends(get_db)):
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.entity_id == entity_id)
        .order_by(ChatMessage.created_at)
        .all()
    )


@router.post(
    "/{entity_id}/chat",
    response_model=ChatMessageOut,
    status_code=201,
    dependencies=[Depends(require_entity_access())],
    summary="Post chat message",
)
def post_message(
    entity_id: str, body: ChatMessageCreate,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    msg = ChatMessage(entity_id=entity_id, user_id=user.id, text=body.text)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@router.post(
    "/{entity_id}/chat/{message_id}/reply",
    response_model=ChatMessageOut,
    status_code=201,
    dependencies=[Depends(require_entity_access())],
    summary="Reply to a chat message",
    responses={404: {"description": "Parent message not found"}},
)
def reply_message(
    entity_id: str, message_id: int, body: ChatReplyCreate,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    msg = db.query(ChatMessage).filter(
        ChatMessage.id == message_id,
        ChatMessage.entity_id == entity_id,
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    reply = ChatReply(message_id=message_id, user_id=user.id, text=body.text)
    db.add(reply)
    db.commit()
    db.refresh(msg)
    return msg


@router.patch(
    "/{entity_id}/chat/{message_id}/flag",
    dependencies=[Depends(require_entity_access())],
    summary="Toggle message flagged",
    responses={404: {"description": "Message not found"}},
)
def flag_message(
    entity_id: str, message_id: int,
    db: Session = Depends(get_db),
):
    msg = db.query(ChatMessage).filter(
        ChatMessage.id == message_id,
        ChatMessage.entity_id == entity_id,
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    msg.is_flagged = not msg.is_flagged
    db.commit()
    return {"id": message_id, "is_flagged": msg.is_flagged}


@router.post(
    "/{entity_id}/chat/{message_id}/forward",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Forward message to another user",
    responses={404: {"description": "Message not found"}},
)
def forward_message(
    entity_id: str, message_id: int, to_user_id: int,
    db: Session = Depends(get_db),
):
    msg = db.query(ChatMessage).filter(
        ChatMessage.id == message_id,
        ChatMessage.entity_id == entity_id,
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    msg.forwarded_to = to_user_id
    db.commit()
    return {"id": message_id, "forwarded_to": to_user_id}
