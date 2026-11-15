# @file    admin/users.py

from __future__ import annotations
import csv
import io
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import require_role, get_current_user
from app.core.security import create_invite_token, hash_password
from app.db.session import get_db
from app.models.models import User, Entity, UserEntityAccess
from app.schemas.schemas import (
    AdminUserOut, AdminUserInviteOut, ResendInviteOut, UserOut,
    InviteUserRequest, UpdateUserRequest, BulkRoleRequest,
)
from app.services.email import (
    EmailSendResult,
    send_invite_email,
)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _invite_email_fields(result: EmailSendResult) -> dict:
    """Translate an EmailSendResult into the partial-success response fields.

    User creation and email delivery are reported independently: a failed
    email never converts a created user into an error. No secrets are
    included — only the stable safe_error_code and a user-safe message.
    """
    if result.success:
        return {
            "email_sent": True,
            "email_status": "accepted",
            "safe_error_code": None,
            "message": "User created and invitation email sent successfully.",
        }
    return {
        "email_sent": False,
        "email_status": "failed",
        "safe_error_code": result.error_code,
        "message": (
            "User created, but the invitation email could not be delivered. "
            "Please resend the invitation."
        ),
    }


def _to_admin_out(db: Session, u: User) -> AdminUserOut:
    sup = db.query(User).filter(User.id == u.supervisor_user_id).first() if u.supervisor_user_id else None
    codes = [r.entity_id for r in db.query(UserEntityAccess).filter(UserEntityAccess.user_id == u.id).all()]
    return AdminUserOut(
        id=u.id, name=u.name, initials=u.initials, color=u.color,
        role=u.role, email=u.email, department=u.department, status=u.status,
        supervisor=UserOut.model_validate(sup) if sup else None,
        entity_codes=codes,
        last_login_at=u.last_login_at, invited_at=u.invited_at,
    )


def _make_initials(first: str, last: str) -> str:
    return ((first[:1] or "?") + (last[:1] or "?")).upper()


def _last_admin_check(db: Session, user_id: int, new_role: str | None = None, disable: bool = False):
    u = db.query(User).filter(User.id == user_id).first()
    if not u or u.role != "admin":
        return
    admins = db.query(User).filter(User.role == "admin", User.status == "active").count()
    if admins <= 1 and (new_role and new_role != "admin" or disable):
        raise HTTPException(status_code=409, detail="Cannot remove or downgrade the last active admin")


@router.get(
    "",
    response_model=List[AdminUserOut],
    summary="List users",
    description="Includes entity access codes and supervisor linkage.",
)
def list_users(db: Session = Depends(get_db)):
    return [
        _to_admin_out(db, u)
        for u in db.query(User).order_by(User.first_name, User.last_name).all()
    ]


@router.post(
    "/invite",
    response_model=AdminUserInviteOut,
    status_code=201,
    summary="Invite user",
    description=(
        "Creates the invited user and entity grants, then attempts to send the "
        "invite email. User creation and email delivery are reported "
        "independently: a failed email returns a partial-success payload "
        "(email_sent=false) rather than failing the whole request."
    ),
    responses={409: {"description": "Email already exists"}},
)
def invite_user(
    body: InviteUserRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if db.query(User).filter(User.email == body.email.lower()).first():
        raise HTTPException(status_code=409, detail="Email already exists")
    initials = _make_initials(body.first_name, body.last_name)
    # ensure unique initials by appending digits if needed
    base = initials
    n = 0
    while db.query(User).filter(User.initials == initials).first():
        n += 1
        initials = (base + str(n))[:5]

    u = User(
        email=body.email.lower(),
        first_name=body.first_name, last_name=body.last_name,
        initials=initials, color="#64748b", role=body.role,
        department=body.department, supervisor_user_id=body.supervisor_user_id,
        status="invited", invited_at=datetime.utcnow(),
    )
    db.add(u)
    db.flush()
    for eid in body.entity_ids:
        if db.query(Entity).filter(Entity.id == eid).first():
            db.add(UserEntityAccess(user_id=u.id, entity_id=eid, granted_by=actor.id))

    token = create_invite_token(u.id, u.email)
    log_change(db, actor=actor, action="user.invite",
               table_name="users", row_pk=str(u.id),
               after={"email": u.email, "role": u.role}, request=request)
    # Persist the user before attempting delivery so a mail-server problem can
    # never roll back a successfully created/invited user.
    db.commit()
    db.refresh(u)

    email_result = send_invite_email(u.email, u.name, token)
    base = _to_admin_out(db, u).model_dump()
    return AdminUserInviteOut(**base, **_invite_email_fields(email_result))


@router.patch(
    "/{user_id}",
    response_model=AdminUserOut,
    summary="Update user",
    description="Partial update; replacing `entity_ids` overwrites grants.",
    responses={404: {"description": "User not found"}, 409: {"description": "Last admin downgrade/disable blocked"}},
)
def update_user(
    user_id: int, body: UpdateUserRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if body.role and body.role != u.role:
        _last_admin_check(db, user_id, new_role=body.role)
    if body.status and body.status == "disabled":
        _last_admin_check(db, user_id, disable=True)

    before = {"role": u.role, "status": u.status, "email": u.email}
    if body.first_name is not None: u.first_name = body.first_name
    if body.last_name  is not None: u.last_name  = body.last_name
    if body.email      is not None: u.email = body.email.lower()
    if body.role       is not None: u.role  = body.role
    if body.status     is not None:
        u.status = body.status
    if body.department is not None: u.department = body.department
    if body.supervisor_user_id is not None:
        u.supervisor_user_id = body.supervisor_user_id

    if body.entity_ids is not None:
        db.query(UserEntityAccess).filter(UserEntityAccess.user_id == u.id).delete()
        for eid in body.entity_ids:
            if db.query(Entity).filter(Entity.id == eid).first():
                db.add(UserEntityAccess(user_id=u.id, entity_id=eid, granted_by=actor.id))

    log_change(db, actor=actor, action="user.update",
               table_name="users", row_pk=str(user_id),
               before=before, after=body.model_dump(exclude_none=True), request=request)
    db.commit()
    db.refresh(u)
    return _to_admin_out(db, u)


@router.delete(
    "/{user_id}",
    status_code=204,
    summary="Delete user",
    responses={404: {"description": "User not found"}, 409: {"description": "Cannot delete self or last admin"}},
)
def delete_user(
    user_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if user_id == actor.id:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")
    _last_admin_check(db, user_id, disable=True)
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    log_change(db, actor=actor, action="user.delete",
               table_name="users", row_pk=str(user_id),
               before={"email": u.email, "role": u.role}, request=request)
    db.delete(u)
    db.commit()


@router.post(
    "/bulk-role",
    summary="Bulk set user role",
    description="Sets the same `role` on many user IDs; last-admin checks per admin user.",
)
def bulk_role(
    body: BulkRoleRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    rows = db.query(User).filter(User.id.in_(body.user_ids)).all()
    for u in rows:
        if u.role == "admin" and body.role != "admin":
            _last_admin_check(db, u.id, new_role=body.role)
    for u in rows:
        u.role = body.role
    log_change(db, actor=actor, action="user.bulk_role",
               table_name="users", row_pk=",".join(map(str, body.user_ids)),
               after={"role": body.role}, request=request)
    db.commit()
    return {"updated": len(rows)}


@router.get(
    "/export.csv",
    summary="Export users CSV",
    description="Returns `text/csv` attachment `users.csv`.",
)
def export_csv(db: Session = Depends(get_db)):
    rows = db.query(User).order_by(User.first_name, User.last_name).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","name","email","role","department","status","entity_codes","supervisor"])
    for u in rows:
        codes = ",".join(r.entity_id for r in
                         db.query(UserEntityAccess).filter(UserEntityAccess.user_id == u.id).all())
        sup = db.query(User).filter(User.id == u.supervisor_user_id).first() if u.supervisor_user_id else None
        w.writerow([u.id, u.name, u.email, u.role, u.department or "", u.status,
                    codes, sup.name if sup else ""])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=users.csv"})


@router.post(
    "/{user_id}/resend-invite",
    response_model=ResendInviteOut,
    summary="Resend invite email",
    responses={400: {"description": "User not in invited state"}, 404: {"description": "User not found"}},
)
def resend_invite(
    user_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if u.status != "invited":
        raise HTTPException(status_code=400, detail="User is not in 'invited' state")
    token = create_invite_token(u.id, u.email)
    log_change(db, actor=actor, action="user.resend_invite",
               table_name="users", row_pk=str(user_id), request=request)
    db.commit()

    email_result = send_invite_email(u.email, u.name, token)
    fields = _invite_email_fields(email_result)
    return ResendInviteOut(
        ok=True,
        email_sent=fields["email_sent"],
        email_status=fields["email_status"],
        safe_error_code=fields["safe_error_code"],
        message=(
            "Invitation email re-sent successfully."
            if email_result.success
            else "The invitation email could not be delivered. Please try again."
        ),
    )
