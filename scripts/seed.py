# @file    scripts/seed.py
#
# Run with:  python -m scripts.seed
#
# Idempotent. Creates schema (dev only), seeds RBAC, ERP catalog, the
# bootstrap admin user, then the demo team / entities / flux data so the
# UI has something to render against before any human invites others.

from __future__ import annotations
import logging
import random
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import Base, SessionLocal, engine
from app.models import models as m  # noqa: F401 (registers all)
from app.models.models import (
    User, Role, Permission, RolePermission, ModuleAccess,
    Entity, UserEntityAccess, EntityAssignment,
    CompanyProfile, Threshold, FluxData, AiCommentary, CaptionVersion,
    MacroFactor, Task, PersonalReminder, ChatMessage,
    ErpCatalog, BrandingSettings,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed")


# â”€â”€ Reference data (mirrors frontend constants/data.js) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TEAM = [
    ("Priya Sharma",  "PS", "#dc2626", "analyst",    "priya@example.com"),
    ("Raj Patel",     "RP", "#7c3aed", "analyst",    "raj@example.com"),
    ("Sara Chen",     "SC", "#0891b2", "analyst",    "sara@example.com"),
    ("David Kim",     "DK", "#d97706", "analyst",    "david@example.com"),
    ("Jane Smith",    "JS", "#2563eb", "supervisor", "jane@example.com"),
    ("Mark Lee",      "ML", "#059669", "supervisor", "mark@example.com"),
    ("Lisa Wang",     "LW", "#be185d", "director",   "lisa@example.com"),
]

COS = [
    {"id":"ACM-US","name":"Acme Corp (US)","region":"North America","tags":["tech","priority"],
     "assignees":["PS"],"supervisor":"JS","sub":"2026-04-18","rev":"2026-04-22",
     "p_type":"YoY","p_from":"Mar 2026","p_to":"Mar 2025","status":"under_review"},
    {"id":"ACM-CA","name":"Acme Canada","region":"North America","tags":["tech"],
     "assignees":["RP"],"supervisor":"JS","sub":"2026-04-22","rev":"2026-04-26",
     "p_type":"MoM","p_from":"Mar 2026","p_to":"Feb 2026","status":"wip"},
    {"id":"BET-US","name":"Beta Holdings","region":"North America","tags":["finance","priority"],
     "assignees":["SC","RP"],"supervisor":"JS","sub":"2026-04-14","rev":"2026-04-18",
     "p_type":"YoY","p_from":"Mar 2026","p_to":"Mar 2025","status":"approved",
     "approval_level":"director","director":"LW"},
    {"id":"ACM-UK","name":"Acme UK","region":"Europe","tags":["tech","emea"],
     "assignees":["PS"],"supervisor":"JS","sub":"2026-04-20","rev":"2026-04-24",
     "p_type":"QoQ","p_from":"Q1 2026","p_to":"Q4 2025","status":"under_review"},
    {"id":"ACM-DE","name":"Acme GmbH","region":"Europe","tags":["tech","emea"],
     "assignees":["DK"],"supervisor":"JS","sub":"2026-04-25","rev":"2026-04-28",
     "p_type":"YoY","p_from":"Mar 2026","p_to":"Mar 2025","status":"rejected"},
    {"id":"ACM-IN","name":"Acme India","region":"APAC","tags":["tech","apac"],
     "assignees":["PS","SC"],"supervisor":"JS","sub":"2026-04-16","rev":"2026-04-20",
     "p_type":"MoM","p_from":"Mar 2026","p_to":"Feb 2026","status":"assigned"},
]

PL_CAPTIONS = [
    "Revenue","Consulting Revenue","Product Revenue","Total Revenue",
    "Cost of Revenue","Gross Profit","Salaries & Wages","Professional Services",
    "Software & Technology","Travel & Entertainment","Depreciation",
    "Total OpEx","Operating Income","Interest Expense","Income Tax","Net Income",
]
BS_CAPTIONS = [
    "Cash","Accounts Receivable","Prepaid Expenses","Total Current Assets",
    "PP&E","Intangibles","Total Assets","Accounts Payable","Accrued Liabilities",
    "Total Current Liabilities","Long-Term Debt","Total Liabilities","Equity",
]


def _is_bold(c: str) -> bool:
    return c.startswith("Total") or c in {"Revenue","Gross Profit","Operating Income","Net Income","Equity"}


def _hash(s: str) -> int:
    h = 0
    for ch in s:
        h = ((h << 5) - h) + ord(ch)
        h &= 0xFFFFFFFF
    return abs(h)


# â”€â”€ Roles / Permissions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
ROLES = [
    ("admin",      "Admin",      "Full system access"),
    ("supervisor", "Supervisor", "Reviews and approves flux"),
    ("analyst",    "Analyst",    "Prepares flux analysis"),
    ("director",   "Director",   "Final approver (Supervisor's Boss)"),
]

PERMISSIONS = [
    # Flux Review
    ("flux.view",            "Flux Review", "View flux data"),
    ("flux.edit_ai",         "Flux Review", "Edit AI commentary"),
    ("flux.signoff",         "Flux Review", "Sign off captions"),
    ("flux.signoff_all",     "Flux Review", "Bulk sign-off"),
    ("flux.submit",          "Flux Review", "Submit entity package"),
    # Workflow
    ("workflow.approve",     "Workflow",    "Approve entity"),
    ("workflow.reject",      "Workflow",    "Reject entity"),
    ("workflow.delegate",    "Workflow",    "Delegate entity"),
    ("workflow.send_director","Workflow",   "Send for director approval"),
    # Comments
    ("comments.create",      "Comments",    "Add comments / @mention"),
    ("comments.resolve",     "Comments",    "Resolve comments"),
    # Tasks
    ("tasks.create",         "Tasks",       "Create tasks"),
    ("tasks.assign",         "Tasks",       "Assign tasks"),
    ("tasks.update_status",  "Tasks",       "Update own task status"),
    # Settings
    ("settings.thresholds",  "Settings",    "Edit thresholds"),
    ("settings.profile",     "Settings",    "Edit company profile"),
    ("settings.periods",     "Settings",    "Edit periods"),
    ("settings.macro",       "Settings",    "Edit macro factors"),
    # Reports
    ("reports.export",       "Reports",     "Export reports"),
    # Admin
    ("admin.users.manage",   "Administration", "Manage users"),
    ("admin.rbac.manage",    "Administration", "Manage roles/permissions"),
    ("admin.entities.manage","Administration", "Manage entities"),
    ("admin.branding.manage","Administration", "Manage branding"),
    ("admin.audit.view",     "Administration", "View audit logs"),
    ("admin.erp.manage",     "Administration", "Manage ERP integrations"),
    ("admin.ontology.manage","Administration", "Manage ontology mapping"),
]

ROLE_PERMS = {
    "analyst":    ["flux.view","flux.submit","comments.create",
                   "tasks.update_status","reports.export"],
    "supervisor": ["flux.view","flux.edit_ai","flux.signoff","flux.signoff_all",
                   "workflow.approve","workflow.reject","workflow.delegate","workflow.send_director",
                   "comments.create","comments.resolve",
                   "tasks.create","tasks.assign","tasks.update_status",
                   "settings.thresholds","settings.profile","settings.periods","settings.macro",
                   "reports.export"],
    "director":   ["flux.view","workflow.approve","workflow.reject",
                   "comments.create","reports.export"],
    # admin always has all (synthesised at runtime)
}

ROLE_MODULES = {
    "analyst":    ["dashboard","flux_review","fincalendar","settings"],
    "supervisor": ["dashboard","flux_review","fincalendar","flux_settings"],
    "director":   ["dashboard","flux_review","approval_queue"],
    "admin":      ["dashboard","admin_users","admin_config","admin_integrations",
                   "admin_ontology","admin_audit","admin_support"],
}

ERP_CATALOG = [
    ("sap_s4",       "SAP S/4HANA",          "Enterprise ERP",        "ðŸ¢", "#1976d2", "oauth2"),
    ("oracle_cloud", "Oracle ERP Cloud",     "Cloud ERP suite",       "ðŸŸ§", "#ea580c", "oauth2"),
    ("netsuite",     "Oracle NetSuite",      "Cloud ERP for SaaS",    "ðŸ“Š", "#16a34a", "oauth2"),
    ("dynamics365",  "Microsoft Dynamics 365","Finance & Operations", "ðŸªŸ", "#2563eb", "oauth2"),
    ("bc",           "Business Central",     "SMB ERP",               "ðŸ“¦", "#0ea5e9", "oauth2"),
    ("quickbooks",   "QuickBooks Online",    "Cloud accounting",      "QB", "#2ca01c", "oauth2"),
    ("workday",      "Workday Financials",   "HR + Finance",          "ðŸ’¼", "#7c3aed", "oauth2"),
]


def seed_rbac(db: Session) -> dict[str, Role]:
    log.info("Seeding roles & permissions ...")
    role_map: dict[str, Role] = {}
    for key, label, desc in ROLES:
        r = db.query(Role).filter(Role.key == key).first()
        if not r:
            r = Role(key=key, label=label, description=desc)
            db.add(r)
            db.flush()
        role_map[key] = r

    perm_map: dict[str, Permission] = {}
    for key, group, label in PERMISSIONS:
        p = db.query(Permission).filter(Permission.key == key).first()
        if not p:
            p = Permission(key=key, group=group, label=label)
            db.add(p)
            db.flush()
        perm_map[key] = p

    for role_key, perm_keys in ROLE_PERMS.items():
        role = role_map[role_key]
        existing = {rp.permission_id for rp in
                    db.query(RolePermission).filter(RolePermission.role_id == role.id).all()}
        for pk in perm_keys:
            p = perm_map[pk]
            if p.id not in existing:
                db.add(RolePermission(role_id=role.id, permission_id=p.id))

    for role_key, modules in ROLE_MODULES.items():
        role = role_map[role_key]
        existing = {ma.module_key for ma in
                    db.query(ModuleAccess).filter(ModuleAccess.role_id == role.id).all()}
        for mk in modules:
            if mk not in existing:
                db.add(ModuleAccess(role_id=role.id, module_key=mk))

    db.commit()
    return role_map


def _split_admin_name(full: str) -> tuple[str, str]:
    """Split `ADMIN_NAME` (e.g. 'System Admin') into first/last names.
    Falls back to ('System', 'Admin') when only one token is provided."""
    parts = (full or "").strip().split()
    if not parts:
        return "System", "Admin"
    if len(parts) == 1:
        return parts[0], "Admin"
    return parts[0], " ".join(parts[1:])


def seed_admin(db: Session) -> User:
    log.info("Seeding bootstrap admin %s ...", settings.ADMIN_EMAIL)
    u = db.query(User).filter(User.email == settings.ADMIN_EMAIL.lower()).first()
    if not u:
        first, last = _split_admin_name(settings.ADMIN_NAME)
        u = User(
            email=settings.ADMIN_EMAIL.lower(),
            password=hash_password(settings.ADMIN_PASSWORD),
            first_name=first, last_name=last,
            initials="AD", color="#1e293b", role="admin",
            status="active",
        )
        db.add(u)
        db.commit()
    elif not u.password:
        u.password = hash_password(settings.ADMIN_PASSWORD)
        u.status = "active"
        db.commit()
    return u


def seed_team(db: Session) -> dict[str, User]:
    log.info("Seeding demo team ...")
    out: dict[str, User] = {}
    for name, init, color, role, email in TEAM:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            parts = name.split()
            first = parts[0]
            last = " ".join(parts[1:]) if len(parts) > 1 else parts[0]
            u = User(
                email=email,
                first_name=first, last_name=last,
                initials=init, color=color, role=role,
                password=hash_password("Demo@12345"),
                status="active",
            )
            db.add(u)
            db.flush()
        out[init] = u
    db.commit()
    return out


def seed_erp_catalog(db: Session) -> None:
    log.info("Seeding ERP catalog ...")
    for key, name, desc, icon, color, auth in ERP_CATALOG:
        if not db.query(ErpCatalog).filter(ErpCatalog.key == key).first():
            db.add(ErpCatalog(key=key, name=name, description=desc,
                              icon=icon, color=color, auth_type=auth))
    db.commit()


def seed_branding(db: Session) -> None:
    if not db.query(BrandingSettings).first():
        db.add(BrandingSettings(primary_color="#16a34a"))
        db.commit()


def _mk_flux_rows(entity_id: str, p_from: str, p_to: str, statement: str,
                  captions: list[str]) -> list[FluxData]:
    rows: list[FluxData] = []
    for i, c in enumerate(captions):
        s = _hash(entity_id + c + str(i)) * 2654435761 & 0xFFFFFFFFFFFFFFFF
        cy   = round(((s % 800000) + 200000) * (2.5 if _is_bold(c) else 1.0))
        comp = round(cy * (0.6 + ((s % 40) / 100)))
        v    = cy - comp
        vp   = 0 if comp == 0 else round((v / abs(comp)) * 100)
        rows.append(FluxData(
            entity_id=entity_id, statement=statement,
            period_from=p_from, period_to=p_to, caption=c,
            current_value=Decimal(cy), comp_value=Decimal(comp),
            pct_variance=Decimal(vp),
            is_material=(abs(v) > 80000) or (abs(vp) > 15),
            is_bold=_is_bold(c),
            sort_order=i,
        ))
    return rows


def _mk_ai(entity_id: str, captions: list[str]) -> list[AiCommentary]:
    out: list[AiCommentary] = []
    for c in captions:
        s = _hash(entity_id + c)
        score = min(98, 60 + (s % 38))
        out.append(AiCommentary(
            entity_id=entity_id, caption=c,
            concise_text=f"{c} variance is within model expectations.",
            detailed_text=f"{c} â€” drivers: volume/mix and rate/price; see GL trace for details.",
            ai_score=score,
        ))
    return out


def seed_entities(db: Session, team: dict[str, User]) -> None:
    log.info("Seeding demo entities + flux data ...")
    for c in COS:
        sup = team[c["supervisor"]]
        director = team.get(c.get("director")) if c.get("approval_level") == "director" else None
        e = db.query(Entity).filter(Entity.id == c["id"]).first()
        if not e:
            e = Entity(
                id=c["id"], name=c["name"], region=c["region"],
                supervisor_id=sup.id,
                director_id=director.id if director else None,
                approval_level=c.get("approval_level", "supervisor"),
                status=c.get("status", "assigned"),
                tags=c["tags"],
                period_type=c["p_type"], period_from=c["p_from"], period_to=c["p_to"],
                sub_due_date=date.fromisoformat(c["sub"]),
                rev_due_date=date.fromisoformat(c["rev"]),
                currency="USD", fiscal_year_end="Dec 31", is_active=True,
            )
            db.add(e)
            db.flush()

            # Default thresholds
            db.add(Threshold(entity_id=e.id, statement="pl",
                             pct_value=10, abs_value=100000))
            db.add(Threshold(entity_id=e.id, statement="bs",
                             pct_value=5,  abs_value=75000))

            # Profile stub
            db.add(CompanyProfile(entity_id=e.id, industry="Software",
                                  geography=c["region"], revenue_range="$10Mâ€“$50M",
                                  entity_type="C-Corp",
                                  brief=f"Demo profile for {c['name']}"))

            # User access (analyst scoping)
            for init in c["assignees"]:
                u = team[init]
                db.add(UserEntityAccess(user_id=u.id, entity_id=e.id, granted_by=sup.id))

            # Flux + AI commentary
            for row in _mk_flux_rows(e.id, c["p_from"], c["p_to"], "pl", PL_CAPTIONS):
                db.add(row)
            for row in _mk_flux_rows(e.id, c["p_from"], c["p_to"], "bs", BS_CAPTIONS):
                db.add(row)
            for ai in _mk_ai(e.id, PL_CAPTIONS + BS_CAPTIONS):
                db.add(ai)
            for cap in PL_CAPTIONS + BS_CAPTIONS:
                db.add(CaptionVersion(entity_id=e.id, caption=cap, version=0))

            # A few macro factors
            db.add(MacroFactor(entity_id=e.id, category="Industry", icon="ðŸ“ˆ",
                               text=f"{c['region']} market dynamics ...",
                               is_custom=False, created_by=sup.id))

    db.commit()


def main() -> None:
    log.info("Creating tables (dev) ...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        seed_rbac(db)
        admin = seed_admin(db)
        team = seed_team(db)
        seed_erp_catalog(db)
        seed_branding(db)
        seed_entities(db, team)
        log.info("Done. Bootstrap admin: %s / (env ADMIN_PASSWORD)", admin.email)
    finally:
        db.close()


if __name__ == "__main__":
    main()
