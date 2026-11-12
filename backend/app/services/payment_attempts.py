"""Durable state transitions for provider-backed payment attempts."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PaymentAttempt

ACTIVE_STATUSES = {"RESERVED", "PROVIDER_REQUESTED", "PERSISTENCE_PENDING", "RECONCILIATION_REQUIRED"}


def is_expected_reservation_conflict(error: IntegrityError) -> bool:
    """Identify only the active transfer/operation uniqueness race."""
    constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if constraint == "uq_payment_attempt_active_transfer_operation":
        return True
    message = str(error.orig).lower()
    return "payment_attempts.transfer_id, payment_attempts.operation" in message


def matches_reservation(attempt: PaymentAttempt, *, user_id: str, idempotency_key: str, fingerprint: str, amount_minor: int, currency: str, return_url: str, cancel_url: str) -> bool:
    return (
        attempt.user_id == user_id
        and attempt.idempotency_key == idempotency_key
        and attempt.request_fingerprint == fingerprint
        and attempt.amount_minor == amount_minor
        and attempt.currency == currency
        and attempt.return_url == return_url
        and attempt.cancel_url == cancel_url
    )


def reserve_payment_order(
    db: Session,
    *,
    transfer_id: str,
    user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    amount_minor: int,
    currency: str,
    return_url: str,
    cancel_url: str,
) -> PaymentAttempt:
    existing = db.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.transfer_id == transfer_id,
            PaymentAttempt.operation == "payment-order",
        )
    )
    if existing:
        return existing
    attempt = PaymentAttempt(
        transfer_id=transfer_id,
        user_id=user_id,
        idempotency_key=idempotency_key,
        operation="payment-order",
        request_fingerprint=request_fingerprint,
        amount_minor=amount_minor,
        currency=currency,
        return_url=return_url,
        cancel_url=cancel_url,
        provider_request_id=f"settlemate-{uuid4().hex}",
        status="RESERVED",
    )
    db.add(attempt)
    db.flush()
    return attempt


def mark_attempt(db: Session, attempt_id: str, status: str, **values: object) -> PaymentAttempt:
    attempt = db.get(PaymentAttempt, attempt_id)
    if attempt is None:
        raise RuntimeError("payment attempt reservation disappeared")
    attempt.status = status
    for key, value in values.items():
        setattr(attempt, key, value)
    if status == "COMPLETED":
        attempt.completed_at = datetime.now(UTC)
    db.commit()
    return attempt
