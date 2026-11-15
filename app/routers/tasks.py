# @file    tasks.py

import logging
from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import (
    get_current_user, require_role, require_entity_access,
    visible_entity_ids,
)
from app.db.session import get_db
from app.models.models import Task, TaskReminder, TaskRecurrence, User
from app.schemas.schemas import TaskOut, TaskCreate, TaskUpdate
from app.services.notification_service import (
    NOTIF_TASK_ASSIGNED,
    NOTIF_TASK_REASSIGNED,
    notify_users,
)

_log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{entity_id}/tasks",
    response_model=List[TaskOut],
    dependencies=[Depends(require_entity_access())],
    summary="List tasks for entity",
    description="Tasks assigned on this entity, ordered by due date.",
)
def get_tasks(entity_id: str, db: Session = Depends(get_db)):
    return (
        db.query(Task)
        .filter(Task.entity_id == entity_id)
        .order_by(Task.due_date)
        .all()
    )


@router.post(
    "/{entity_id}/tasks",
    response_model=TaskOut,
    status_code=201,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Create task",
    description="Optional nested reminder and recurrence configuration.",
)
def create_task(
    entity_id: str, body: TaskCreate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    task = Task(
        entity_id   = entity_id,
        title       = body.title,
        description = body.description,
        assignee_id = body.assignee_id,
        due_date    = body.due_date,
        status      = body.status,
        priority    = body.priority,
        phase       = body.phase,
        is_recurring= bool(body.recurrence),
    )
    db.add(task)
    db.flush()

    if body.reminder:
        r = body.reminder
        reminder = TaskReminder(
            task_id     = task.id,
            days_before = r.get("days_before", 1),
            remind_time = r.get("time", "09:00:00"),
            email       = r.get("email", ""),
        )
        db.add(reminder)

    if body.recurrence:
        rc = body.recurrence
        recur = TaskRecurrence(
            task_id      = task.id,
            frequency    = rc.get("frequency", "monthly"),
            day_of_month = rc.get("day_of_month"),
        )
        db.add(recur)

    log_change(db, actor=user, action="task.create",
               table_name="tasks", row_pk=str(task.id),
               after={"title": task.title, "assignee_id": task.assignee_id,
                      "due_date": str(task.due_date)},
               request=request)

    try:
        notify_users(
            db,
            [task.assignee_id],
            notif_type=NOTIF_TASK_ASSIGNED,
            title="Task Assigned",
            message=f"You have been assigned a task: {task.title}",
            actor_id=user.id,
            entity_id=entity_id,
            link_url=f"/supervisor/flux-review?entity={entity_id}",
        )
    except Exception:
        _log.warning("Task assign notification failed (non-blocking)", exc_info=False)

    db.commit()
    db.refresh(task)
    return task


@router.patch(
    "/{entity_id}/tasks/{task_id}",
    response_model=TaskOut,
    dependencies=[Depends(require_entity_access())],
    summary="Update task",
    description="Analysts may only update **status** on their own tasks; supervisors+ can edit all fields.",
    responses={403: {"description": "Analyst restriction"}, 404: {"description": "Task not found"}},
)
def update_task(
    entity_id: str, task_id: int, body: TaskUpdate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id, Task.entity_id == entity_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if user.role == "analyst":
        if task.assignee_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
        if any([body.title, body.description, body.assignee_id, body.due_date, body.priority, body.phase]):
            raise HTTPException(status_code=403, detail="Analyst can only update status")

    before = {"status": task.status, "assignee_id": task.assignee_id, "due_date": str(task.due_date)}
    old_assignee_id = task.assignee_id

    for field in ("title", "description", "status", "assignee_id", "due_date", "priority", "phase"):
        v = getattr(body, field, None)
        if v is not None:
            setattr(task, field, v)

    log_change(db, actor=user, action="task.update",
               table_name="tasks", row_pk=str(task_id),
               before=before, after=body.model_dump(exclude_none=True), request=request)

    # Notify new assignee if reassigned
    if body.assignee_id is not None and body.assignee_id != old_assignee_id:
        try:
            notify_users(
                db,
                [task.assignee_id],
                notif_type=NOTIF_TASK_REASSIGNED,
                title="Task Reassigned",
                message=f"You have been assigned a task: {task.title}",
                actor_id=user.id,
                entity_id=entity_id,
                link_url=f"/supervisor/flux-review?entity={entity_id}",
            )
        except Exception:
            _log.warning("Task reassign notification failed (non-blocking)", exc_info=False)

    db.commit()
    db.refresh(task)
    return task


@router.delete(
    "/{entity_id}/tasks/{task_id}",
    status_code=204,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Delete task",
    responses={404: {"description": "Task not found"}},
)
def delete_task(
    entity_id: str, task_id: int, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id, Task.entity_id == entity_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    log_change(db, actor=user, action="task.delete",
               table_name="tasks", row_pk=str(task_id),
               before={"title": task.title}, request=request)
    db.delete(task)
    db.commit()


@router.post(
    "/{entity_id}/tasks/{task_id}/reminder",
    dependencies=[Depends(require_entity_access())],
    summary="Mark task reminder as sent",
    description="Dev/demo hook to flip reminder sent flags.",
    responses={404: {"description": "Task not found"}},
)
def trigger_reminder(entity_id: str, task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id, Task.entity_id == entity_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.reminder:
        task.reminder.is_sent = True
        task.reminder.sent_at = datetime.utcnow()
        db.commit()
    return {"task_id": task_id, "reminder_triggered": True}


@router.get(
    "/{entity_id}/tasks/all",
    response_model=List[TaskOut],
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="List all tasks (supervisor view)",
    description="Same as entity task list but explicitly supervisor/director/admin gated.",
)
def get_all_tasks(entity_id: str, db: Session = Depends(get_db)):
    return db.query(Task).filter(Task.entity_id == entity_id).order_by(Task.due_date).all()
