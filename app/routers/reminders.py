# @file    reminders.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.models import PersonalReminder, User
from app.schemas.schemas import PersonalReminderOut, PersonalReminderCreate

router = APIRouter()


@router.get(
    "/personal",
    response_model=List[PersonalReminderOut],
    summary="List my personal reminders",
    description="Current user only; ordered by due date.",
)
def get_personal_reminders(
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    return (
        db.query(PersonalReminder)
        .filter(PersonalReminder.user_id == user.id)
        .order_by(PersonalReminder.due_date)
        .all()
    )


@router.post(
    "/personal",
    response_model=PersonalReminderOut,
    status_code=201,
    summary="Create personal reminder",
)
def add_personal_reminder(
    body: PersonalReminderCreate,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    r = PersonalReminder(
        user_id     = user.id,
        text        = body.text,
        due_date    = body.due_date,
        remind_time = body.remind_time,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.patch(
    "/personal/{reminder_id}/done",
    summary="Toggle reminder done",
    responses={404: {"description": "Reminder not found"}},
)
def toggle_done(
    reminder_id: int,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    r = db.query(PersonalReminder).filter(
        PersonalReminder.id == reminder_id,
        PersonalReminder.user_id == user.id,
    ).first()
    if not r:
        raise HTTPException(status_code=404, detail="Reminder not found")
    r.is_done = not r.is_done
    db.commit()
    return {"id": reminder_id, "is_done": r.is_done}


@router.delete(
    "/personal/{reminder_id}",
    status_code=204,
    summary="Delete personal reminder",
    responses={404: {"description": "Reminder not found"}},
)
def delete_reminder(
    reminder_id: int,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    r = db.query(PersonalReminder).filter(
        PersonalReminder.id == reminder_id,
        PersonalReminder.user_id == user.id,
    ).first()
    if not r:
        raise HTTPException(status_code=404, detail="Reminder not found")
    db.delete(r)
    db.commit()
