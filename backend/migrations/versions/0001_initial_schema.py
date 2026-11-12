"""create persistent SettleMate schema"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    common = [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now())]
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("email", sa.String(320), nullable=False, unique=True), sa.Column("display_name", sa.String(120), nullable=False), sa.Column("password_hash", sa.String(256), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False), *common)
    op.create_table("groups", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(160), nullable=False), sa.Column("created_by_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), *common)
    op.create_table("group_members", sa.Column("id", sa.String(36), primary_key=True), sa.Column("group_id", sa.String(36), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("role", sa.String(32), nullable=False), sa.UniqueConstraint("group_id", "user_id"), *common)
    op.create_table("expenses", sa.Column("id", sa.String(36), primary_key=True), sa.Column("group_id", sa.String(36), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False), sa.Column("paid_by_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("description", sa.String(240), nullable=False), sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("status", sa.String(32), nullable=False), *common)
    op.create_table("expense_splits", sa.Column("id", sa.String(36), primary_key=True), sa.Column("expense_id", sa.String(36), sa.ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.UniqueConstraint("expense_id", "user_id"), *common)
    op.create_table("settlements", sa.Column("id", sa.String(36), primary_key=True), sa.Column("group_id", sa.String(36), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("currency", sa.String(3), nullable=False), *common)
    op.create_table("transfers", sa.Column("id", sa.String(36), primary_key=True), sa.Column("settlement_id", sa.String(36), sa.ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False), sa.Column("from_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("to_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("status", sa.String(32), nullable=False), *common)
    op.create_table("payments", sa.Column("id", sa.String(36), primary_key=True), sa.Column("transfer_id", sa.String(36), sa.ForeignKey("transfers.id"), nullable=False, unique=True), sa.Column("provider", sa.String(32), nullable=False), sa.Column("provider_order_id", sa.String(128)), sa.Column("provider_capture_id", sa.String(128)), sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("status", sa.String(32), nullable=False), *common)
    op.create_table("agent_runs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("role", sa.String(32), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("input_data", sa.JSON(), nullable=False), sa.Column("output_data", sa.JSON(), nullable=False), *common)
    op.create_table("audit_logs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id")), sa.Column("action", sa.String(120), nullable=False), sa.Column("entity_type", sa.String(80), nullable=False), sa.Column("entity_id", sa.String(64), nullable=False), sa.Column("metadata_json", sa.JSON(), nullable=False), *common)
    op.create_table("webhook_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("provider_event_id", sa.String(160), nullable=False, unique=True), sa.Column("event_type", sa.String(120), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("verified", sa.Boolean(), nullable=False), sa.Column("processed", sa.Boolean(), nullable=False), *common)
    op.create_table("idempotency_keys", sa.Column("key", sa.String(160), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("response_json", sa.JSON(), nullable=False), *common)


def downgrade() -> None:
    for table in ("idempotency_keys", "webhook_events", "audit_logs", "agent_runs", "payments", "transfers", "settlements", "expense_splits", "expenses", "group_members", "groups", "users"):
        op.drop_table(table)
