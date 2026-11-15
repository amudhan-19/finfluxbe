# Shared OpenAPI / Swagger fragments for route decorators.

API_DESCRIPTION = """
Tick & Tie **FinFlux** — flux analysis backend (FastAPI + MySQL).

## Authentication

1. Call **POST /api/auth/login** with email and password.
2. Copy `access_token` from the response.
3. In Swagger UI click **Authorize**, choose **HTTPBearer**, paste: `Bearer <access_token>` (or paste the raw token if your client adds the prefix).

The refresh token is stored in an **httpOnly** cookie (`finflux_rt`) scoped to `/api/auth`; the SPA uses **POST /api/auth/refresh** with that cookie.

## Roles

**analyst** · **supervisor** · **director** · **admin** — `admin` bypasses most role checks. Entity-scoped routes also enforce **user–entity access** for non-admins.

## Common HTTP status codes

| Code | Meaning |
|------|---------|
| 401 | Missing or invalid JWT |
| 403 | Authenticated but wrong role or no access to entity |
| 404 | Resource not found |
| 409 | Business rule conflict (e.g. unresolved comments block approval, last admin, ontology locked) |
"""

OPENAPI_TAGS = [
    {"name": "Health", "description": "Liveness check (no auth)."},
    {
        "name": "Auth",
        "description": "Login, refresh, logout, current user, invite acceptance, password reset. "
        "Only `/login` and token-based invite/reset are unauthenticated for their specific flows.",
    },
    {
        "name": "Entities",
        "description": "Legal entities: list, detail, and status updates (status patch requires supervisor, director, or admin).",
    },
    {"name": "Flux", "description": "P&L / BS flux rows and AI commentary for an entity period."},
    {"name": "Comments", "description": "Threaded caption comments; new comments bump caption version and stale sign-offs."},
    {"name": "Sign-Offs", "description": "Per-caption and bulk sign-off; supervisor, director, or admin."},
    {"name": "Tasks", "description": "Entity tasks, reminders, recurrence; analysts have limited update rights."},
    {"name": "FinChat", "description": "Per-entity chat messages, replies, flags, and supervisor forward."},
    {"name": "Export", "description": "Build a JSON export package and write an export audit row."},
    {"name": "Macro Factors", "description": "Entity macro / geopolitical factor cards."},
    {
        "name": "Profile & Settings",
        "description": "Company profile, materiality thresholds, period metadata, and supervisor delegation.",
    },
    {
        "name": "Workflow",
        "description": "Submit, approve, reject, send-for-director, and director queue. "
        "Approve / send-for-director return **409** if unresolved comments exist.",
    },
    {"name": "Personal Reminders", "description": "User-scoped reminders (not tied to a single entity row)."},
    {"name": "Admin: Users", "description": "Invite, update, delete users, bulk role, CSV export, resend invite. **admin** only."},
    {"name": "Admin: RBAC", "description": "Roles, permissions matrix, module access, license stub. **admin** only."},
    {"name": "Admin: Entities", "description": "CRUD legal entities and default thresholds. **admin** only."},
    {"name": "Admin: Branding", "description": "Tenant primary color and logo upload. **admin** only."},
    {"name": "Admin: Audit", "description": "Paginated audit log query. **admin** only."},
    {
        "name": "Admin: Integrations",
        "description": "ERP catalog, entity↔ERP mapping, encrypted API credentials, mocked test/sync in v1. **admin** only.",
    },
    {
        "name": "Admin: Ontology",
        "description": "Caption ↔ ERP mapping; **409** until GL + Sub-GL + Transaction credentials are verified. **admin** only.",
    },
    {
        "name": "Admin: Support",
        "description": "Contact channels, ticket intake (audit stub), and keyword AI assistant. **admin** only.",
    },
]
