# @file    admin/rbac.py

from __future__ import annotations
from typing import List, Dict
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import require_role, get_current_user
from app.db.session import get_db
from app.models.models import (
    Role, Permission, RolePermission, ModuleAccess, User,
)
from app.schemas.schemas import (
    RoleOut, PermissionOut, ModuleAccessUpdate, PermissionMatrixUpdate,
)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


@router.get(
    "/roles",
    response_model=List[RoleOut],
    summary="List RBAC roles",
)
def list_roles(db: Session = Depends(get_db)):
    return db.query(Role).order_by(Role.id).all()


@router.get(
    "/permissions",
    response_model=List[PermissionOut],
    summary="List permissions",
    description="Ordered by group then label.",
)
def list_permissions(db: Session = Depends(get_db)):
    return db.query(Permission).order_by(Permission.group, Permission.label).all()


@router.get(
    "/role-permissions",
    summary="Permission matrix (read)",
    description="Map of role key → permission keys; **admin** includes all keys.",
)
def role_permissions(db: Session = Depends(get_db)) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    rows = (
        db.query(Role.key, Permission.key)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .all()
    )
    for role_key, perm_key in rows:
        out.setdefault(role_key, []).append(perm_key)
    # admin always has all
    all_perms = [p.key for p in db.query(Permission).all()]
    out["admin"] = all_perms
    return out


@router.put(
    "/role-permissions",
    summary="Update role permissions",
    description="Replaces all permissions for a role (except admin, which is fixed).",
    responses={400: {"description": "Cannot edit admin role"}, 404: {"description": "Role not found"}},
)
def update_role_permissions(
    body: PermissionMatrixUpdate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if body.role_key == "admin":
        raise HTTPException(status_code=400, detail="Admin permissions are always enabled")
    role = db.query(Role).filter(Role.key == body.role_key).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    perms = db.query(Permission).filter(Permission.key.in_(body.permission_keys)).all()
    db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
    for p in perms:
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    log_change(db, actor=actor, action="rbac.permissions.update",
               table_name="role_permissions", row_pk=body.role_key,
               after={"permission_keys": body.permission_keys}, request=request)
    db.commit()
    return {"role_key": body.role_key, "count": len(perms)}


@router.get(
    "/module-access",
    summary="Module access matrix (read)",
    description="Map of role key → enabled FinFlux module keys.",
)
def module_access(db: Session = Depends(get_db)) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    rows = (
        db.query(Role.key, ModuleAccess.module_key)
        .join(ModuleAccess, ModuleAccess.role_id == Role.id)
        .all()
    )
    for role_key, mod_key in rows:
        out.setdefault(role_key, []).append(mod_key)
    return out


@router.put(
    "/module-access",
    summary="Update module access",
    responses={400: {"description": "Cannot edit admin modules"}, 404: {"description": "Role not found"}},
)
def update_module_access(
    body: ModuleAccessUpdate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if body.role_key == "admin":
        raise HTTPException(status_code=400, detail="Admin modules are always enabled")
    role = db.query(Role).filter(Role.key == body.role_key).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    db.query(ModuleAccess).filter(ModuleAccess.role_id == role.id).delete()
    for m in body.modules:
        db.add(ModuleAccess(role_id=role.id, module_key=m))
    log_change(db, actor=actor, action="rbac.modules.update",
               table_name="module_access", row_pk=body.role_key,
               after={"modules": body.modules}, request=request)
    db.commit()
    return {"role_key": body.role_key, "modules": body.modules}


@router.get(
    "/licenses",
    summary="License seat summary (stub)",
    description="Static renewal date + seats used from active users.",
)
def licenses(db: Session = Depends(get_db)):
    total = db.query(User).filter(User.status != "disabled").count()
    return {"seats_total": 200, "seats_used": total, "renewal_date": "2027-04-30"}
