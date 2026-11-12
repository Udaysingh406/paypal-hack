"""Archive duplicate idempotency keys and restore global active uniqueness."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003_global_idempotency_recovery"
down_revision = "0002_scope_idempotency_keys"
branch_labels = None
depends_on = None

_ACTIVE_UNIQUE = "uq_idempotency_keys_key_0003"


def _require_columns(bind) -> None:
    required = {
        "id", "key", "user_id", "operation", "resource_id",
        "request_fingerprint", "response_json", "created_at", "updated_at",
    }
    actual = {column["name"] for column in inspect(bind).get_columns("idempotency_keys")}
    missing = required - actual
    if missing:
        raise RuntimeError(f"idempotency_keys is missing required columns: {sorted(missing)}")


def _drop_matching_uniques(bind, columns: set[str]) -> None:
    inspector = inspect(bind)
    for constraint in inspector.get_unique_constraints("idempotency_keys"):
        if set(constraint.get("column_names") or ()) == columns:
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, "idempotency_keys", type_="unique")
    for index in inspector.get_indexes("idempotency_keys"):
        if index.get("unique") and set(index.get("column_names") or ()) == columns:
            name = index.get("name")
            if name:
                op.drop_index(name, table_name="idempotency_keys")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("idempotency_keys"):
        raise RuntimeError("idempotency_keys must exist before this migration")
    if inspector.has_table("idempotency_key_archive"):
        raise RuntimeError("idempotency_key_archive already exists")
    _require_columns(bind)

    null_timestamps = bind.execute(sa.text(
        """SELECT 1 FROM idempotency_keys
           WHERE created_at IS NULL OR updated_at IS NULL LIMIT 1"""
    )).first()
    if null_timestamps:
        raise RuntimeError("idempotency_keys contains null timestamps")

    op.create_table(
        "idempotency_key_archive",
        sa.Column("original_id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("original_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archive_reason", sa.String(240), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("financial_integrity_classification", sa.String(32), nullable=False),
        sa.UniqueConstraint("original_id", name="uq_idempotency_key_archive_original_id"),
    )

    duplicate_predicate = """ik.key IN (
        SELECT key FROM idempotency_keys GROUP BY key HAVING COUNT(*) > 1
    )"""
    archive_sql = sa.text(f"""
        INSERT INTO idempotency_key_archive (
            original_id, key, user_id, operation, resource_id,
            request_fingerprint, response_json, original_created_at,
            original_updated_at, archive_reason, financial_integrity_classification
        )
        SELECT ik.id, ik.key, ik.user_id, ik.operation, ik.resource_id,
               ik.request_fingerprint, ik.response_json, ik.created_at,
               ik.updated_at,
               'duplicate active key; provider status unverified',
               CASE
                 WHEN p.id IS NULL THEN 'unresolved_provider_status'
                 WHEN p.amount_minor <> t.amount_minor OR p.currency <> t.currency
                   THEN 'amount_mismatch'
                 ELSE 'consistent'
               END
        FROM idempotency_keys ik
        LEFT JOIN payments p ON p.id::text = ik.response_json->>'payment_id'
        LEFT JOIN transfers t ON t.id::text = ik.resource_id
        WHERE {duplicate_predicate}
    """)
    bind.execute(archive_sql)

    expected = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM idempotency_keys ik WHERE {duplicate_predicate}"
    )).scalar_one()
    archived = bind.execute(sa.text("SELECT COUNT(*) FROM idempotency_key_archive")).scalar_one()
    if expected != archived:
        raise RuntimeError(f"archive verification failed: expected {expected}, copied {archived}")
    missing = bind.execute(sa.text(f"""
        SELECT 1 FROM idempotency_keys ik
        WHERE {duplicate_predicate}
          AND NOT EXISTS (
              SELECT 1 FROM idempotency_key_archive a
              WHERE a.original_id = ik.id
          )
        LIMIT 1
    """)).first()
    if missing:
        raise RuntimeError("archive verification failed: an original row was not copied")

    bind.execute(sa.text(
        "DELETE FROM idempotency_keys WHERE id IN "
        "(SELECT original_id FROM idempotency_key_archive)"
    ))

    _drop_matching_uniques(bind, {"user_id", "key", "operation", "resource_id"})
    _drop_matching_uniques(bind, {"key"})
    op.create_unique_constraint(_ACTIVE_UNIQUE, "idempotency_keys", ["key"])
    op.alter_column("idempotency_keys", "created_at", nullable=False)
    op.alter_column("idempotency_keys", "updated_at", nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("idempotency_key_archive"):
        return
    archived = bind.execute(sa.text("SELECT 1 FROM idempotency_key_archive LIMIT 1")).first()
    if archived:
        raise RuntimeError(
            "Cannot downgrade while idempotency_key_archive contains financial history"
        )
    op.drop_table("idempotency_key_archive")
