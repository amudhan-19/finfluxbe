# @file    auth.py
#
# Authentication: login / refresh / logout / me / accept-invite / forgot / reset.

from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user, visible_entity_ids
from app.core.security import (
    create_access_token, create_refresh_token,
    create_invite_token, create_reset_token,
    decode_token, hash_password, verify_password,
)
from app.db.session import get_db
from app.models.models import User, RefreshToken
from app.schemas.schemas import (
    LoginRequest, LoginResponse, UserMe,
    AcceptInviteRequest, ForgotPasswordRequest, ResetPasswordRequest,
)
from app.services.email import send_reset_email


router = APIRouter()


REFRESH_COOKIE = "finflux_rt"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TTL_DAYS * 86400,
        path="/api/auth",
        domain=settings.COOKIE_DOMAIN or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth", domain=settings.COOKIE_DOMAIN or None)


def _to_user_me(db: Session, user: User) -> UserMe:
    visible = visible_entity_ids(db, user)
    return UserMe(
        id=user.id, name=user.name, initials=user.initials, color=user.color,
        role=user.role, email=user.email, department=user.department,
        status=user.status, visible_entity_ids=visible,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Login",
    description="Validate email/password, issue JWT **access_token**, set httpOnly refresh cookie `finflux_rt`, return user profile including **visible_entity_ids**.",
    responses={401: {"description": "Invalid email or password"}, 403: {"description": "Account not active"}},
)
def login(body: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.password or ""):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.status != "active":
        raise HTTPException(status_code=403, detail=f"Account is {user.status}")

    user.last_login_at = datetime.utcnow()

    jti = secrets.token_urlsafe(24)
    refresh = create_refresh_token(user.id, jti)
    db.add(RefreshToken(
        user_id=user.id,
        jti=jti,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TTL_DAYS),
        ip_address=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    db.commit()

    _set_refresh_cookie(response, refresh)
    access = create_access_token(user.id, user.role)
    return LoginResponse(access_token=access, user=_to_user_me(db, user))


@router.post(
    "/refresh",
    response_model=LoginResponse,
    summary="Rotate refresh token",
    description="Reads `finflux_rt` cookie, validates stored JTI, revokes old refresh row, issues new access + refresh tokens.",
    responses={401: {"description": "Missing, invalid, or revoked refresh token"}},
)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    rt = request.cookies.get(REFRESH_COOKIE)
    if not rt:
        raise HTTPException(status_code=401, detail="No refresh token")
    payload = decode_token(rt, "refresh")
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    jti = payload.get("jti")
    user_id = int(payload["sub"])
    row = db.query(RefreshToken).filter(RefreshToken.jti == jti).first()
    if not row or row.revoked_at or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    # Rotate
    row.revoked_at = datetime.utcnow()
    user = db.query(User).filter(User.id == user_id).first()
    if not user or user.status != "active":
        db.commit()
        raise HTTPException(status_code=401, detail="User unavailable")

    new_jti = secrets.token_urlsafe(24)
    new_refresh = create_refresh_token(user.id, new_jti)
    db.add(RefreshToken(
        user_id=user.id,
        jti=new_jti,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TTL_DAYS),
        ip_address=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    db.commit()

    _set_refresh_cookie(response, new_refresh)
    access = create_access_token(user.id, user.role)
    return LoginResponse(access_token=access, user=_to_user_me(db, user))


@router.post(
    "/logout",
    summary="Logout",
    description="Revokes current refresh token (if present) and clears the refresh cookie.",
)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    rt = request.cookies.get(REFRESH_COOKIE)
    if rt:
        payload = decode_token(rt, "refresh")
        if payload:
            row = db.query(RefreshToken).filter(RefreshToken.jti == payload.get("jti")).first()
            if row and not row.revoked_at:
                row.revoked_at = datetime.utcnow()
                db.commit()
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.get(
    "/me",
    response_model=UserMe,
    summary="Current user",
    description="Returns profile, role, and visible entity IDs for the Bearer access token.",
    responses={401: {"description": "Missing or invalid access token"}},
)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _to_user_me(db, user)


@router.post(
    "/accept-invite",
    response_model=LoginResponse,
    summary="Accept invite",
    description="Consumes invite JWT, sets password, activates user, starts session (access + refresh cookie).",
    responses={400: {"description": "Invalid or expired invite token"}, 404: {"description": "User not found"}},
)
def accept_invite(body: AcceptInviteRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    payload = decode_token(body.token, "invite")
    if not payload:
        raise HTTPException(status_code=400, detail="Invalid or expired invite")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password = hash_password(body.password)
    user.status = "active"
    user.last_login_at = datetime.utcnow()

    jti = secrets.token_urlsafe(24)
    refresh = create_refresh_token(user.id, jti)
    db.add(RefreshToken(
        user_id=user.id, jti=jti,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TTL_DAYS),
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    _set_refresh_cookie(response, refresh)
    access = create_access_token(user.id, user.role)
    return LoginResponse(access_token=access, user=_to_user_me(db, user))


@router.post(
    "/forgot-password",
    summary="Request password reset",
    description="Always returns `{ok: true}` to avoid email enumeration; sends reset mail when user exists and is active.",
)
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if user and user.status == "active":
        token = create_reset_token(user.id, user.email)
        send_reset_email(user.email, user.name, token)
    return {"ok": True}  # don't leak existence


@router.post(
    "/reset-password",
    summary="Complete password reset",
    description="Consumes reset JWT and updates password hash.",
    responses={400: {"description": "Invalid or expired reset token"}, 404: {"description": "User not found"}},
)
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.token, "reset")
    if not payload:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password = hash_password(body.password)
    db.commit()
    return {"ok": True}
