# @file    deps.py
#
# FastAPI dependencies for auth + RBAC.
#
#   get_current_user           — resolves Bearer token to a User row (401 otherwise)
#   require_role(*roles)       — coarse role gate
#   require_perm("flux.signoff")— fine-grained permission gate
#   require_entity_access      — per-entity authorisation (analyst sees only assigned)

from __future__ import annotations
from typing import Iterable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.session import get_db
from app.models.models import (
    User,
    Entity,
    UserEntityAccess,
    Role,
    Permission,
    RolePermission,
)


_bearer = HTTPBearer(auto_error=False)


def _unauth(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    token: str | None = None
    if creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
    elif "Authorization" in request.headers:
        h = request.headers["Authorization"]
        if h.lower().startswith("bearer "):
            token = h.split(" ", 1)[1]

    if not token:
        raise _unauth()

    payload = decode_token(token, "access")
    if not payload:
        raise _unauth("Invalid or expired token")

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise _unauth("User no longer exists")
    if user.status != "active":
        raise _unauth(f"Account is {user.status}")
    return user


def require_role(*roles: str):
    """Dependency factory: requires user.role in roles."""
    role_set = set(roles)

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role == "admin":
            return user
        if user.role not in role_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' not permitted (required: {sorted(role_set)})",
            )
        return user

    return _check


def require_perm(perm_key: str):
    """Dependency factory: requires user's role to have perm_key.

    Admin always passes. Permission table is consulted otherwise.
    """

    def _check(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if user.role == "admin":
            return user

        ok = (
            db.query(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .filter(Role.key == user.role, Permission.key == perm_key)
            .first()
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{perm_key}' denied for role '{user.role}'",
            )
        return user

    return _check


def can_access_entity(db: Session, user: User, entity_id: str) -> bool:
    """Returns True if `user` has any visibility into `entity_id`."""
    if user.role in ("admin", "supervisor", "director"):
        return True
    row = (
        db.query(UserEntityAccess)
        .filter(UserEntityAccess.user_id == user.id, UserEntityAccess.entity_id == entity_id)
        .first()
    )
    return row is not None


def require_entity_access(entity_id_param: str = "entity_id"):
    """Dependency factory: enforces user can access path-param `entity_id`."""

    def _check(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        entity_id = request.path_params.get(entity_id_param)
        if not entity_id:
            return user
        if not db.query(Entity).filter(Entity.id == entity_id).first():
            raise HTTPException(status_code=404, detail="Entity not found")
        if not can_access_entity(db, user, entity_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No access to entity {entity_id}",
            )
        return user

    return _check


def visible_entity_ids(db: Session, user: User) -> list[str] | None:
    """Returns the set of entity ids the user can see, or None to mean 'all'."""
    if user.role in ("admin", "supervisor", "director"):
        return None
    rows = db.query(UserEntityAccess.entity_id).filter(UserEntityAccess.user_id == user.id).all()
    return [r[0] for r in rows]
