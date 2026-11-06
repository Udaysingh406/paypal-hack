# SettleMate

## Setup

```powershell
Copy-Item .env.example .env
docker compose up -d db
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements-dev.txt
alembic -c backend\alembic.ini upgrade head
uvicorn app.main:app --app-dir backend --reload
```

In another terminal:

```powershell
npm --prefix frontend install
npm --prefix frontend run dev
```

Set `NEXT_PUBLIC_API_URL` in `.env.local` for the frontend if the API is not at
`http://localhost:8000`.

## Validation

```powershell
$env:PYTHONPATH="backend"
pytest backend/tests
npm --prefix frontend run lint
npm --prefix frontend run build
```

The API persists users, groups, members, expenses, splits, settlements,
transfers, payments, agent runs, audit logs, webhook events, and idempotency
keys through SQLAlchemy. Run `alembic -c backend\alembic.ini upgrade head` for
PostgreSQL deployments; SQLite table creation is enabled only for local
development.

All monetary fields are integer minor units. PayPal Sandbox is a verified
merchant checkout boundary, not escrow or arbitrary peer-to-peer payouts.

Authentication uses signed bearer tokens for this MVP. Every group-scoped
endpoint checks membership before reading or mutating financial records.
Payment creation requires a debtor-owned `Idempotency-Key`; payment completion
is accepted only from a verified PayPal server response or verified webhook.
