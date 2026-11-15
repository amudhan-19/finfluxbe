# @file    export.py

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_entity_access
from app.db.session import get_db
from app.models.models import ExportLog, FluxData, AiCommentary, Entity, User
from app.schemas.schemas import ExportRequest

router = APIRouter()


@router.post(
    "/{entity_id}/export",
    dependencies=[Depends(require_entity_access())],
    summary="Export flux report (JSON)",
    description="Builds variance + commentary payload for `pl`, `bs`, or `both`; logs **export_log** and audit.",
    responses={404: {"description": "Entity not found"}},
)
def export_report(
    entity_id: str, body: ExportRequest, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")

    stmts = ["pl", "bs"] if body.statement == "both" else [body.statement]
    report = {"entity": entity_id, "mode": body.mode, "data": {}}

    for stmt in stmts:
        rows = (
            db.query(FluxData)
            .filter(
                FluxData.entity_id   == entity_id,
                FluxData.statement   == stmt,
                FluxData.period_from == e.period_from,
                FluxData.period_to   == e.period_to,
            )
            .order_by(FluxData.sort_order)
            .all()
        )
        result_rows = []
        for row in rows:
            ai = db.query(AiCommentary).filter(
                AiCommentary.entity_id == entity_id,
                AiCommentary.caption   == row.caption,
            ).first()
            commentary = None
            if ai:
                commentary = ai.edited_text or (
                    ai.concise_text if body.mode == "concise" else ai.detailed_text
                )
            result_rows.append({
                "caption":       row.caption,
                "current":       float(row.current_value),
                "comp":          float(row.comp_value),
                "variance":      float(row.current_value - row.comp_value),
                "pct":           float(row.pct_variance or 0),
                "is_material":   row.is_material,
                "commentary":    commentary,
            })
        report["data"][stmt] = result_rows

    log = ExportLog(
        entity_id   = entity_id,
        exported_by = user.id,
        mode        = body.mode,
        statement   = body.statement,
    )
    db.add(log)
    log_change(db, actor=user, action="export.run",
               table_name="export_log", row_pk=entity_id,
               after={"mode": body.mode, "statement": body.statement}, request=request)
    db.commit()
    return report
