# @file    admin/support.py
#
# Admin Support: static contact channels, ticket intake (audit + stub ref),
# keyword-based AI replies (v1 — no external LLM).

from __future__ import annotations

import logging
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import require_role, get_current_user
from app.db.session import get_db
from app.models.models import User
from app.schemas.schemas import (
    AdminAiChatRequest,
    AdminAiChatResponse,
    AdminSupportTicketCreate,
    AdminSupportTicketOut,
    SupportChannelOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])

_SUPPORT_CHANNELS: List[SupportChannelOut] = [
    SupportChannelOut(
        id="email",
        label="Email Support",
        value="support@tickandtie.io",
        description="Within 4 business hours",
        color="#16a34a",
        bg="#dcfce7",
    ),
    SupportChannelOut(
        id="phone",
        label="Enterprise Phone",
        value="+1 800 845 8443",
        description="Mon–Fri, 9 AM – 6 PM IST",
        color="#2563eb",
        bg="#dbeafe",
    ),
    SupportChannelOut(
        id="kb",
        label="Knowledge Base",
        value="docs.tickandtie.io",
        description="Guides, API docs, ontology schema",
        color="#0891b2",
        bg="#cffafe",
    ),
]


def _ai_reply(message: str) -> str:
    s = message.lower()
    if any(k in s for k in ("erp", "sap", "oracle", "netsuite", "dynamics", "workday")):
        return (
            "For ERP integration: open Integrations, connect the entity to its ERP, "
            "then add GL, Sub-GL and Transaction API keys. Ontology unlocks once all three layers verify."
        )
    if any(k in s for k in ("ontolog", "caption", "map")):
        return (
            "Ontology Mapping shows a fixed set of T-Flux captions. For each entity you map ERP GL / Sub-GL codes, "
            "optionally rename for display, and choose whether Flux Review shows T-Flux, ERP, or Custom captions."
        )
    if any(k in s for k in ("user", "invite", "role", "permission")):
        return (
            "Open Users to invite teammates. Analysts must have a supervisor; the supervisor must be assigned "
            "to the same entities you give the analyst access to."
        )
    if any(k in s for k in ("workflow", "approval", "director", "sign", "off")):
        return (
            "Analyst submits → Supervisor signs off captions → Approve. For director-level entities, "
            "supervisor sends for final approval; director approves on their queue."
        )
    if any(k in s for k in ("threshold", "material")):
        return (
            "Each entity has P&L and BS thresholds (% / $ / both / and / or). "
            "Defaults: P&L 10% / $100K, BS 5% / $75K. Edit in Flux Settings."
        )
    if any(k in s for k in ("ai", "commentary", "insight")):
        return (
            "AI commentary is auto-generated per caption. Supervisors can edit it inline. "
            "Each entity also has macro / industry factors that feed the AI."
        )
    return (
        "I don't have a script for that yet — please raise a ticket on the left "
        "and the support team will get back to you."
    )


@router.get(
    "/channels",
    response_model=List[SupportChannelOut],
    summary="Support contact channels",
)
def list_channels():
    return _SUPPORT_CHANNELS


@router.post(
    "/tickets",
    response_model=AdminSupportTicketOut,
    status_code=201,
    summary="Raise a support ticket (stub reference)",
)
def create_ticket(
    body: AdminSupportTicketCreate,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ref = f"TF-{secrets.randbelow(9000) + 1000}"
    log_change(
        db,
        actor=actor,
        action="support.ticket",
        table_name="support_tickets",
        row_pk=ref,
        after={"subject": body.subject, "priority": body.priority},
        request=request,
    )
    db.commit()
    return AdminSupportTicketOut(
        id=0,
        reference=ref,
        message="Ticket recorded. Our team will follow up by email.",
    )


@router.post(
    "/ai-chat",
    response_model=AdminAiChatResponse,
    summary="AI support keyword assistant (v1)",
)
def ai_chat(body: AdminAiChatRequest):
    try:
        reply = _ai_reply(body.message)
        return AdminAiChatResponse(reply=reply)
    except Exception:
        logger.exception("ai_chat failed")
        raise HTTPException(
            status_code=503,
            detail="AI support is temporarily unavailable. Please try again or raise a ticket.",
        ) from None
