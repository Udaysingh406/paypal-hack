"""store idempotency scope and enforce globally unique keys"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_scope_idempotency_keys"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("idempotency_keys"):
        raise RuntimeError("0001_initial_schema must create idempotency_keys before this migration")
    if inspector.has_table("idempotency_keys_new"):
        raise RuntimeError("temporary idempotency_keys_new table already exists")
    columns = {column["name"] for column in inspector.get_columns("idempotency_keys")}
    required = {"key", "user_id", "response_json", "created_at", "updated_at"}
    missing = required - columns
    if missing:
        raise RuntimeError(f"idempotency_keys is missing required columns: {sorted(missing)}")
    duplicate_key = bind.execute(
        sa.text(
            "SELECT 1 FROM idempotency_keys "
            "GROUP BY key HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_key:
        raise RuntimeError("idempotency_keys contains duplicate keys")
    op.create_table(
        "idempotency_keys_new",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("key"),
    )
    old_keys = sa.table(
        "idempotency_keys",
        sa.column("key", sa.String),
        sa.column("user_id", sa.String),
        sa.column("response_json", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    new_keys = sa.table(
        "idempotency_keys_new",
        sa.column("id", sa.String),
        sa.column("key", sa.String),
        sa.column("user_id", sa.String),
        sa.column("operation", sa.String),
        sa.column("resource_id", sa.String),
        sa.column("request_fingerprint", sa.String),
        sa.column("response_json", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    rows = []
    for row in bind.execute(sa.select(old_keys)).mappings():
        rows.append({
            "id": str(uuid4()),
            "key": row["key"],
            "user_id": row["user_id"],
            # Legacy scope cannot be reconstructed from the original schema.
            "operation": "legacy",
            "resource_id": "",
            "request_fingerprint": "",
            "response_json": row["response_json"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    if rows:
        bind.execute(new_keys.insert(), rows)
    op.drop_table("idempotency_keys")
    op.rename_table("idempotency_keys_new", "idempotency_keys")


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("idempotency_keys"):
        raise RuntimeError("idempotency_keys does not exist")
    if bind.execute(sa.text("SELECT 1 FROM idempotency_keys LIMIT 1")).first():
        raise RuntimeError(
            "Cannot downgrade populated idempotency_keys: the legacy schema cannot preserve migration metadata"
        )
    op.create_table(
        "idempotency_keys_old",
        sa.Column("key", sa.String(160), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.drop_table("idempotency_keys")
    op.rename_table("idempotency_keys_old", "idempotency_keys")
