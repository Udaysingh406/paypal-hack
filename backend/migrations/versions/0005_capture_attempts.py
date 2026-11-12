"""Add durable single-capture attempt history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0005_capture_attempts"
down_revision = "0004_payment_attempts"
branch_labels = None
depends_on = None

_ACTIVE = "'RESERVED', 'PROVIDER_REQUESTED', 'METADATA_PERSISTED', 'PERSISTENCE_PENDING', 'RECONCILIATION_REQUIRED'"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    required = {"users", "transfers", "payments", "payment_attempts"}
    missing = sorted(required - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError(f"0004_payment_attempts must be applied first; missing: {missing}")
    if inspector.has_table("capture_attempts"):
        raise RuntimeError("capture_attempts already exists")

    existing = {c["name"] for c in inspector.get_unique_constraints("payment_attempts")}
    if "uq_payment_attempt_context" not in existing:
        op.create_unique_constraint("uq_payment_attempt_context", "payment_attempts", ["id", "transfer_id", "provider_order_id"])

    op.create_table(
        "capture_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payment_id", sa.String(36), sa.ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("transfer_id", sa.String(36), sa.ForeignKey("transfers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("parent_attempt_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_order_id", sa.String(128), nullable=False),
        sa.Column("provider_request_id", sa.String(108), nullable=False),
        sa.Column("provider_capture_id", sa.String(128), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reconciliation_state", sa.String(32), nullable=False),
        sa.Column("provider_capture_status", sa.String(32), nullable=True),
        sa.Column("error_classification", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_attempt_id", "transfer_id", "provider_order_id"],
            ["payment_attempts.id", "payment_attempts.transfer_id", "payment_attempts.provider_order_id"],
            ondelete="RESTRICT",
            name="fk_capture_attempt_parent_context",
        ),
        sa.UniqueConstraint("provider_request_id", name="uq_capture_attempt_provider_request_id"),
        sa.UniqueConstraint("provider_capture_id", name="uq_capture_attempt_provider_capture_id"),
        sa.CheckConstraint("amount_minor > 0", name="ck_capture_attempt_positive_amount"),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'PROVIDER_REQUESTED', 'METADATA_PERSISTED', 'PERSISTENCE_PENDING', 'COMPLETED', 'FAILED', 'RECONCILIATION_REQUIRED', 'CANCELLED')",
            name="ck_capture_attempt_status",
        ),
        sa.CheckConstraint(
            "reconciliation_state IN ('NOT_REQUIRED', 'PENDING', 'REQUIRED', 'RESOLVED', 'BLOCKED')",
            name="ck_capture_attempt_reconciliation_state",
        ),
        sa.CheckConstraint("status NOT IN ('PROVIDER_REQUESTED', 'METADATA_PERSISTED', 'PERSISTENCE_PENDING', 'COMPLETED') OR provider_order_id IS NOT NULL", name="ck_capture_attempt_provider_order"),
        sa.CheckConstraint("status <> 'COMPLETED' OR (provider_capture_id IS NOT NULL AND completed_at IS NOT NULL)", name="ck_capture_attempt_completed_fields"),
        sa.CheckConstraint("status <> 'RECONCILIATION_REQUIRED' OR reconciliation_state IN ('REQUIRED', 'BLOCKED')", name="ck_capture_attempt_reconciliation_required"),
    )
    op.create_index("ix_capture_attempts_payment_id", "capture_attempts", ["payment_id"])
    op.create_index("ix_capture_attempts_transfer_id", "capture_attempts", ["transfer_id"])
    op.create_index("ix_capture_attempts_provider_order_id", "capture_attempts", ["provider_order_id"])
    op.create_index("ix_capture_attempts_parent_attempt_id", "capture_attempts", ["parent_attempt_id"])
    op.create_index("uq_capture_attempt_active_payment", "capture_attempts", ["payment_id"], unique=True, postgresql_where=sa.text(f"status IN ({_ACTIVE})"))
    op.create_index("uq_capture_attempt_completed_payment", "capture_attempts", ["payment_id"], unique=True, postgresql_where=sa.text("status = 'COMPLETED'"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("capture_attempts"):
        return
    if bind.execute(sa.text("SELECT 1 FROM capture_attempts LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade populated capture_attempts: capture history would be lost")
    for name in ("uq_capture_attempt_completed_payment", "uq_capture_attempt_active_payment", "ix_capture_attempts_parent_attempt_id", "ix_capture_attempts_provider_order_id", "ix_capture_attempts_transfer_id", "ix_capture_attempts_payment_id"):
        op.drop_index(name, table_name="capture_attempts")
    op.drop_table("capture_attempts")
    inspector = inspect(bind)
    if "uq_payment_attempt_context" in {c["name"] for c in inspector.get_unique_constraints("payment_attempts")}:
        op.drop_constraint("uq_payment_attempt_context", "payment_attempts", type_="unique")
