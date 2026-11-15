# FinFlux Backend

FastAPI + SQLAlchemy + MySQL. Role-based authentication (JWT) and RBAC enforcement
for Analyst / Supervisor / Director / Admin. Admin Console covers user / entity
management, RBAC, ERP integration (mocked in v1), ontology mapping and audit logs.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
cp .env.example .env       # then edit DB creds + JWT_SECRET + FERNET_KEY

# Generate strong secrets
python -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())"
```

## Database

```bash
# Create the DB once
mysql -u root -p -e "CREATE DATABASE finflux DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"

# Seed tables, RBAC, bootstrap admin, ERP catalog, demo entities, flux data
python -m scripts.seed
```

The seed script is idempotent — it skips existing rows. Schema is created via
`Base.metadata.create_all` in dev (`APP_ENV=development`); production should
move to Alembic migrations.

## Run

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`. Authorise with the bootstrap admin:

```
POST /api/auth/login  { "email": "admin@example.com", "password": "Admin@12345" }
```

Take the `access_token` from the response and click **Authorize** in Swagger UI.

## Role rules (enforced server-side)

- **Analyst** — sees only entities granted via `user_entity_access`; can submit, comment, update own task status, export.
- **Supervisor** — full visibility; sign-off, edit AI, approve/reject/delegate, manage tasks/thresholds/profile/macro.
- **Director** — sees entities where `entities.director_id = me`, approves on `pending_director`.
- **Admin** — full system access; only role allowed under `/api/admin/*`.

## Key invariants

- Adding a new comment increments `caption_versions.version` and marks all sign-offs for that caption `is_stale = true`.
- `POST /entities/{id}/approve` rejects with **409** if any unresolved comments exist on the entity.
- `PUT /admin/ontology/{id}` rejects with **409** until the entity's GL + Sub-GL + Transaction API credentials are all `verified` (mocked sync flips them).
- Cannot delete or downgrade the last active admin.
- All admin mutations + status changes write to `audit_logs(before_json, after_json, ip)`.

## Layout

```
app/
  main.py
  core/        config / security (JWT) / deps (RBAC) / audit / crypto (Fernet)
  db/          session
  models/      models.py (single file, ~30 tables)
  schemas/     schemas.py (Pydantic)
  routers/
    auth.py
    entities.py  flux.py  comments.py  signoffs.py  tasks.py
    chat.py  macro.py  profile.py  reminders.py  export.py  workflow.py
    admin/
      users.py  rbac.py  entities.py  branding.py  audit.py
      integrations.py  ontology.py
  services/    email.py
scripts/
  seed.py      idempotent seeder (RBAC + admin + demo data)
```
