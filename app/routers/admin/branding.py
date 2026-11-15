# @file    admin/branding.py

import os
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import require_role, get_current_user
from app.db.session import get_db
from app.models.models import BrandingSettings, User
from app.schemas.schemas import BrandingOut

router = APIRouter(dependencies=[Depends(require_role("admin"))])

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _get_or_create(db: Session) -> BrandingSettings:
    b = db.query(BrandingSettings).first()
    if not b:
        b = BrandingSettings()
        db.add(b)
        db.commit()
        db.refresh(b)
    return b


@router.get(
    "",
    response_model=BrandingOut,
    summary="Get tenant branding",
    description="Creates default row on first read.",
)
def get_branding(db: Session = Depends(get_db)):
    return _get_or_create(db)


@router.put(
    "",
    response_model=BrandingOut,
    summary="Update primary color",
    description="Optional query param `primary_color` (hex). Updates branding row and audit.",
)
def set_branding(
    primary_color: str | None = None, request: Request = None,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    b = _get_or_create(db)
    if primary_color:
        b.primary_color = primary_color
    b.updated_by = actor.id
    log_change(db, actor=actor, action="branding.update",
               table_name="branding_settings", row_pk=str(b.id),
               after={"primary_color": primary_color}, request=request)
    db.commit()
    db.refresh(b)
    return b


@router.post(
    "/logo",
    response_model=BrandingOut,
    summary="Upload logo image",
    description="Multipart file; must be `image/*`. Stored under configured `UPLOAD_DIR`.",
    responses={400: {"description": "Not an image content-type"}},
)
async def upload_logo(
    request: Request, file: UploadFile = File(...),
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Logo must be an image")
    suffix = Path(file.filename or "logo.png").suffix or ".png"
    target = UPLOAD_DIR / f"branding_logo_{int(datetime.utcnow().timestamp())}{suffix}"
    with target.open("wb") as f:
        f.write(await file.read())
    b = _get_or_create(db)
    b.logo_url = f"/uploads/{target.name}"
    b.updated_by = actor.id
    log_change(db, actor=actor, action="branding.logo",
               table_name="branding_settings", row_pk=str(b.id),
               after={"logo_url": b.logo_url}, request=request)
    db.commit()
    db.refresh(b)
    return b
