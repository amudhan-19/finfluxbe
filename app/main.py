# @file    main.py
#
# FinFlux Backend — FastAPI entry point.

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.core.config import settings
from app.core.openapi_extra import API_DESCRIPTION, OPENAPI_TAGS
from app.db.session import Base, engine
from app.db.migrations_runtime import align_business_central_schema
import app.models.models  # noqa: F401  — register all ORM models
from app.services.erp.auto_sync_scheduler import bc_auto_sync_scheduler

_log = logging.getLogger(__name__)

from app.routers import (
    auth,
    entities,
    flux,
    flux_ai,
    flux_generation_diagnostics,
    comments,
    signoffs,
    tasks,
    chat,
    export,
    macro,
    profile,
    reminders,
    workflow,
    notifications,
)
from app.routers.admin import (
    users  as admin_users,
    rbac   as admin_rbac,
    entities as admin_entities,
    branding as admin_branding,
    audit  as admin_audit,
    integrations as admin_integrations,
    ontology as admin_ontology,
    ontology_automap as admin_ontology_automap,
    flux_generate as admin_flux_generate,
    flux_generate_qb as admin_flux_generate_qb,
    bc_flux_diagnostics as admin_bc_flux_diagnostics,
    support as admin_support,
    business_central as admin_business_central,
    business_central_auto_sync as admin_bc_auto_sync,
    quickbooks as admin_quickbooks,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """FastAPI lifespan — starts the BC auto-sync scheduler when enabled,
    and always cleanly stops it on shutdown so reload cycles don't leak
    background threads. Failures are logged and swallowed so a broken
    scheduler config can never block uvicorn from booting."""
    try:
        bc_auto_sync_scheduler.start()
    except Exception:  # pragma: no cover - defensive
        _log.exception("BC auto-sync scheduler start failed in lifespan.")
    try:
        yield
    finally:
        try:
            bc_auto_sync_scheduler.stop()
        except Exception:  # pragma: no cover - defensive
            _log.exception("BC auto-sync scheduler stop failed in lifespan.")


app = FastAPI(
    title="FinFlux API",
    description=API_DESCRIPTION.strip(),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=OPENAPI_TAGS,
    lifespan=_lifespan,
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["HTTPBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste `access_token` from POST /api/auth/login (Swagger adds Bearer prefix).",
    }
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Auto-create tables in development (production should run migrations CLI:
#   python -m app.db.migrations_runtime
# ).
if settings.is_dev:
    Base.metadata.create_all(bind=engine)

# Idempotent runtime schema alignment for the Business Central / ERP
# normalization tables. Adds any missing columns + indexes — never drops
# data, never raises (failures are logged). Safe in dev and prod.
align_business_central_schema(engine)


# Auth (no role gate)
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])

# Domain routers (each enforces RBAC inside via Depends)
app.include_router(entities.router,  prefix="/api/entities",  tags=["Entities"])
app.include_router(flux.router,      prefix="/api/entities",  tags=["Flux"])
app.include_router(flux_ai.router,   prefix="/api/entities",  tags=["Flux AI"])
app.include_router(flux_generation_diagnostics.router,
                   prefix="/api/entities",
                   tags=["Flux Generation"])
app.include_router(comments.router,  prefix="/api/entities",  tags=["Comments"])
app.include_router(signoffs.router,  prefix="/api/entities",  tags=["Sign-Offs"])
app.include_router(tasks.router,     prefix="/api/entities",  tags=["Tasks"])
app.include_router(chat.router,      prefix="/api/entities",  tags=["FinChat"])
app.include_router(export.router,    prefix="/api/entities",  tags=["Export"])
app.include_router(macro.router,     prefix="/api/entities",  tags=["Macro Factors"])
app.include_router(profile.router,   prefix="/api/entities",  tags=["Profile & Settings"])
app.include_router(workflow.router,  prefix="/api/entities",  tags=["Workflow"])
app.include_router(reminders.router,      prefix="/api/reminders",     tags=["Personal Reminders"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications"])

# Admin Console
app.include_router(admin_users.router,        prefix="/api/admin/users",       tags=["Admin: Users"])
app.include_router(admin_rbac.router,         prefix="/api/admin/rbac",        tags=["Admin: RBAC"])
app.include_router(admin_entities.router,     prefix="/api/admin/entities",    tags=["Admin: Entities"])
app.include_router(admin_branding.router,     prefix="/api/admin/branding",    tags=["Admin: Branding"])
app.include_router(admin_audit.router,        prefix="/api/admin/audit-logs",  tags=["Admin: Audit"])
app.include_router(admin_integrations.router, prefix="/api/admin/integrations", tags=["Admin: Integrations"])
app.include_router(admin_business_central.router,
                   prefix="/api/admin/integrations/business-central",
                   tags=["Admin: Integrations — Business Central"])
app.include_router(admin_bc_auto_sync.router,
                   prefix="/api/admin/integrations/business-central",
                   tags=["Admin: Integrations — BC Auto-Sync"])
app.include_router(admin_quickbooks.router,
                   prefix="/api/admin/integrations/quickbooks",
                   tags=["Admin: Integrations — QuickBooks"])
app.include_router(admin_quickbooks.callback_router,
                   prefix="/api/admin/integrations/quickbooks",
                   tags=["Admin: Integrations — QuickBooks OAuth"])
app.include_router(admin_flux_generate_qb.router,
                   prefix="/api/admin/entities",
                   tags=["Admin: Flux Generate — QuickBooks"])
app.include_router(admin_ontology.router,     prefix="/api/admin/ontology",    tags=["Admin: Ontology"])
app.include_router(admin_ontology_automap.router,
                   prefix="/api/admin/entities",
                   tags=["Admin: Ontology — Demo Auto-Map (disabled)"])
app.include_router(admin_flux_generate.router,
                   prefix="/api/admin/entities",
                   tags=["Admin: Flux Generate"])
app.include_router(admin_bc_flux_diagnostics.router,
                   prefix="/api/admin/entities",
                   tags=["Admin: BC Flux Diagnostics"])
app.include_router(admin_support.router,    prefix="/api/admin/support",     tags=["Admin: Support"])


@app.get(
    "/",
    tags=["Health"],
    summary="Health check",
    description="Public liveness endpoint for load balancers and smoke tests.",
)
def health_check():
    return {"status": "ok", "app": "FinFlux API", "version": "2.0.0"}
