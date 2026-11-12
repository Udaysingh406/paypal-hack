from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def uid() -> str:
    return str(uuid4())


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Group(Timestamped, Base):
    __tablename__ = "groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    members: Mapped[list["GroupMember"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    expenses: Mapped[list["Expense"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class GroupMember(Timestamped, Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32), default="member")
    group: Mapped[Group] = relationship(back_populates="members")


class Expense(Timestamped, Base):
    __tablename__ = "expenses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    paid_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String(240))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32), default="active")
    group: Mapped[Group] = relationship(back_populates="expenses")
    splits: Mapped[list["ExpenseSplit"]] = relationship(back_populates="expense", cascade="all, delete-orphan")


class ExpenseSplit(Timestamped, Base):
    __tablename__ = "expense_splits"
    __table_args__ = (UniqueConstraint("expense_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    expense_id: Mapped[str] = mapped_column(ForeignKey("expenses.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    expense: Mapped[Expense] = relationship(back_populates="splits")


class Settlement(Timestamped, Base):
    __tablename__ = "settlements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    currency: Mapped[str] = mapped_column(String(3))
    group: Mapped[Group] = relationship(back_populates="settlements")
    transfers: Mapped[list["Transfer"]] = relationship(back_populates="settlement", cascade="all, delete-orphan")


class Transfer(Timestamped, Base):
    __tablename__ = "transfers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    settlement_id: Mapped[str] = mapped_column(ForeignKey("settlements.id", ondelete="CASCADE"))
    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    settlement: Mapped[Settlement] = relationship(back_populates="transfers")
    payment: Mapped["Payment | None"] = relationship(back_populates="transfer", uselist=False)


class Payment(Timestamped, Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="paypal")
    provider_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_capture_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    transfer: Mapped[Transfer] = relationship(back_populates="payment")


class PaymentAttempt(Timestamped, Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        Index(
            "uq_payment_attempt_active_transfer_operation",
            "transfer_id",
            "operation",
            unique=True,
            postgresql_where=text(
                "status IN ('RESERVED', 'PROVIDER_REQUESTED', 'PERSISTENCE_PENDING', 'RECONCILIATION_REQUIRED')"
            ),
        ),
        CheckConstraint(
            "status IN ('RESERVED', 'PROVIDER_REQUESTED', 'PERSISTENCE_PENDING', 'RECONCILIATION_REQUIRED', 'FAILED', 'CANCELLED', 'COMPLETED')",
            name="ck_payment_attempt_status",
        ),
        UniqueConstraint("id", "transfer_id", "provider_order_id", name="uq_payment_attempt_context"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    return_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    cancel_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="paypal")
    provider_request_id: Mapped[str] = mapped_column(String(108), nullable=False, unique=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RESERVED")
    error_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaptureAttempt(Timestamped, Base):
    __tablename__ = "capture_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_attempt_id", "transfer_id", "provider_order_id"],
            ["payment_attempts.id", "payment_attempts.transfer_id", "payment_attempts.provider_order_id"],
            ondelete="RESTRICT",
            name="fk_capture_attempt_parent_context",
        ),
        Index(
            "uq_capture_attempt_active_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("status IN ('RESERVED', 'PROVIDER_REQUESTED', 'METADATA_PERSISTED', 'PERSISTENCE_PENDING', 'RECONCILIATION_REQUIRED')"),
        ),
        Index("uq_capture_attempt_completed_payment", "payment_id", unique=True, postgresql_where=text("status = 'COMPLETED'")),
        CheckConstraint("amount_minor > 0", name="ck_capture_attempt_positive_amount"),
        CheckConstraint("status IN ('RESERVED', 'PROVIDER_REQUESTED', 'METADATA_PERSISTED', 'PERSISTENCE_PENDING', 'COMPLETED', 'FAILED', 'RECONCILIATION_REQUIRED', 'CANCELLED')", name="ck_capture_attempt_status"),
        CheckConstraint("reconciliation_state IN ('NOT_REQUIRED', 'PENDING', 'REQUIRED', 'RESOLVED', 'BLOCKED')", name="ck_capture_attempt_reconciliation_state"),
        CheckConstraint("status NOT IN ('PROVIDER_REQUESTED', 'METADATA_PERSISTED', 'PERSISTENCE_PENDING', 'COMPLETED') OR provider_order_id IS NOT NULL", name="ck_capture_attempt_provider_order"),
        CheckConstraint("status <> 'COMPLETED' OR (provider_capture_id IS NOT NULL AND completed_at IS NOT NULL)", name="ck_capture_attempt_completed_fields"),
        CheckConstraint("status <> 'RECONCILIATION_REQUIRED' OR reconciliation_state IN ('REQUIRED', 'BLOCKED')", name="ck_capture_attempt_reconciliation_required"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, index=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id", ondelete="RESTRICT"), nullable=False, index=True)
    parent_attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="paypal")
    provider_order_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_request_id: Mapped[str] = mapped_column(String(108), nullable=False, unique=True)
    provider_capture_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RESERVED")
    reconciliation_state: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_REQUIRED")
    provider_capture_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRun(Timestamped, Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="completed")
    input_data: Mapped[dict] = mapped_column(JSON, default=dict)
    output_data: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Timestamped, Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WebhookEvent(Timestamped, Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_event_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class IdempotencyKey(Timestamped, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict] = mapped_column(JSON)
