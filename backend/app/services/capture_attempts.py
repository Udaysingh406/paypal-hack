"""Durable single-capture reservation and persistence helpers."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaptureAttempt, Payment, PaymentAttempt, Transfer
from app.services.payments import validate_capture_response


def reserve_capture(db: Session, payment: Payment, transfer: Transfer) -> CaptureAttempt:
    if payment.transfer_id != transfer.id or payment.provider_capture_id or payment.status != "APPROVAL_PENDING" or transfer.status != "pending":
        raise RuntimeError("payment is not capture-eligible")
    existing = db.scalar(select(CaptureAttempt).where(CaptureAttempt.payment_id == payment.id).order_by(CaptureAttempt.created_at.desc()))
    if existing:
        if existing.status in {"COMPLETED", "METADATA_PERSISTED"}:
            return existing
        raise RuntimeError("capture recovery is required")
    parent = db.scalar(select(PaymentAttempt).where(PaymentAttempt.transfer_id == transfer.id, PaymentAttempt.provider_order_id == payment.provider_order_id).order_by(PaymentAttempt.created_at.desc()))
    if parent is None or parent.provider_order_id is None:
        raise RuntimeError("payment order recovery is required")
    attempt = CaptureAttempt(
        payment_id=payment.id,
        transfer_id=transfer.id,
        parent_attempt_id=parent.id,
        provider=payment.provider,
        provider_order_id=payment.provider_order_id,
        provider_request_id=f"settlemate-capture-{uuid4().hex}",
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        status="RESERVED",
        reconciliation_state="NOT_REQUIRED",
    )
    db.add(attempt)
    db.flush()
    db.commit()
    return attempt


def mark_capture(db: Session, attempt_id: str, status: str, **values: object) -> CaptureAttempt:
    attempt = db.get(CaptureAttempt, attempt_id)
    if attempt is None:
        raise RuntimeError("capture attempt disappeared")
    attempt.status = status
    for key, value in values.items():
        setattr(attempt, key, value)
    if status == "COMPLETED":
        attempt.completed_at = datetime.now(UTC)
    db.commit()
    return attempt


def complete_metadata_capture(db: Session, attempt_id: str) -> tuple[Payment, CaptureAttempt]:
    attempt = db.scalar(select(CaptureAttempt).where(CaptureAttempt.id == attempt_id).with_for_update())
    if attempt is None or attempt.status != "METADATA_PERSISTED" or not attempt.provider_capture_id:
        raise RuntimeError("capture metadata is not recoverable")
    payment = db.scalar(select(Payment).where(Payment.id == attempt.payment_id).with_for_update())
    transfer = db.scalar(select(Transfer).where(Transfer.id == attempt.transfer_id).with_for_update())
    if payment is None or transfer is None or payment.transfer_id != transfer.id:
        raise RuntimeError("capture ownership is invalid")
    if payment.amount_minor != attempt.amount_minor or payment.currency != attempt.currency or transfer.amount_minor != attempt.amount_minor or transfer.currency != attempt.currency:
        raise RuntimeError("capture financial snapshot is invalid")
    if payment.provider_order_id != attempt.provider_order_id or payment.provider_capture_id not in {None, attempt.provider_capture_id}:
        raise RuntimeError("capture provider association conflicts")
    if payment.status not in {"APPROVAL_PENDING", "CAPTURE_PENDING"} or transfer.status != "pending":
        raise RuntimeError("payment is not recoverable")
    payment.provider_capture_id = attempt.provider_capture_id
    payment.status = "COMPLETED"
    transfer.status = "completed"
    attempt.status = "COMPLETED"
    attempt.reconciliation_state = "RESOLVED"
    attempt.completed_at = datetime.now(UTC)
    db.commit()
    return payment, attempt


def reconcile_capture(db: Session, attempt_id: str, provider_reader) -> tuple[Payment, CaptureAttempt]:
    attempt = db.scalar(select(CaptureAttempt).where(CaptureAttempt.id == attempt_id))
    if attempt is None:
        raise RuntimeError("capture attempt not found")
    if attempt.status == "METADATA_PERSISTED":
        return complete_metadata_capture(db, attempt_id)
    if attempt.status == "COMPLETED":
        payment = db.get(Payment, attempt.payment_id)
        return payment, attempt
    if attempt.status != "RECONCILIATION_REQUIRED":
        raise RuntimeError("capture attempt is not awaiting reconciliation")
    order_id = attempt.provider_order_id
    db.rollback()
    try:
        order = provider_reader(order_id)
        capture_id, provider_status = validate_capture_response(order, attempt.amount_minor, attempt.currency)
        if str(order.get("id")) != order_id or provider_status != "COMPLETED":
            raise ValueError("provider capture is not a verified completed match")
    except Exception as exc:
        db.rollback()
        mark_capture(db, attempt_id, "RECONCILIATION_REQUIRED", reconciliation_state="BLOCKED", error_classification=type(exc).__name__[:64])
        raise RuntimeError("provider capture could not be verified") from exc
    mark_capture(db, attempt_id, "METADATA_PERSISTED", reconciliation_state="RESOLVED", provider_capture_id=capture_id, provider_capture_status=provider_status)
    return complete_metadata_capture(db, attempt_id)
