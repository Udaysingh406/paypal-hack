"""Add durable payment-attempt reservations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004_payment_attempts"
down_revision = "0003_global_idempotency_recovery"
branch_labels = None
depends_on = None

_ACTIVE_STATUSES = "'RESERVED', 'PROVIDER_REQUESTED', 'PERSISTENCE_PENDING', 'RECONCILIATION_REQUIRED'"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    required_tables = {"users", "transfers", "idempotency_keys", "idempotency_key_archive"}
    missing = sorted(required_tables - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError(f"0003_global_idempotency_recovery must be applied first; missing: {missing}")
    if inspector.has_table("payment_attempts"):
        raise RuntimeError("payment_attempts already exists")

    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("transfer_id", sa.String(36), sa.ForeignKey("transfers.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("return_url", sa.String(2048), nullable=True),
        sa.Column("cancel_url", sa.String(2048), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_request_id", sa.String(108), nullable=False),
        sa.Column("provider_order_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_classification", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider_request_id", name="uq_payment_attempt_provider_request_id"),
        sa.UniqueConstraint("provider_order_id", name="uq_payment_attempt_provider_order_id"),
        sa.CheckConstraint(
            f"status IN ({_ACTIVE_STATUSES}, 'FAILED', 'CANCELLED', 'COMPLETED')",
            name="ck_payment_attempt_status",
        ),
    )
    op.create_index("ix_payment_attempts_transfer_id", "payment_attempts", ["transfer_id"])
    op.create_index("ix_payment_attempts_user_id", "payment_attempts", ["user_id"])
    op.create_index(
        "uq_payment_attempt_active_transfer_operation",
        "payment_attempts",
        ["transfer_id", "operation"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_ACTIVE_STATUSES})"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("payment_attempts"):
        return
    if bind.execute(sa.text("SELECT 1 FROM payment_attempts LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade populated payment_attempts: attempt history would be lost")
    op.drop_index("uq_payment_attempt_active_transfer_operation", table_name="payment_attempts")
    op.drop_index("ix_payment_attempts_user_id", table_name="payment_attempts")
    op.drop_index("ix_payment_attempts_transfer_id", table_name="payment_attempts")
    op.drop_table("payment_attempts")
