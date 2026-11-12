"""Read-only idempotency and payment-integrity validation."""

from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import IdempotencyKey, Payment, Transfer

RECOVERY_REQUIRED = "Payment recovery required"


def _archive_table(db: Session) -> sa.Table | None:
    bind = db.get_bind()
    if not inspect(bind).has_table("idempotency_key_archive"):
        return None
    return sa.Table("idempotency_key_archive", sa.MetaData(), autoload_with=bind)


def archived_key_exists(db: Session, key: str) -> bool:
    try:
        table = _archive_table(db)
        if table is None:
            raise_recovery_unavailable()
        return db.execute(
            select(table.c.original_id).where(table.c.key == key).limit(1)
        ).first() is not None
    except SQLAlchemyError as exc:
        try:
            raise_recovery_unavailable()
        except HTTPException as error:
            raise error from exc


def raise_recovery_unavailable() -> None:
    raise HTTPException(status_code=503, detail="Payment recovery is unavailable")


def raise_recovery_required() -> None:
    raise HTTPException(status_code=409, detail=RECOVERY_REQUIRED)


def validate_payment_integrity(payment: Payment | None, transfer: Transfer, user_id: str) -> None:
    if payment is None:
        raise_recovery_required()
    if payment.transfer_id != transfer.id or transfer.from_user_id != user_id:
        raise_recovery_required()
    if payment.amount_minor != transfer.amount_minor or payment.currency != transfer.currency:
        raise_recovery_required()
    if transfer.status != "pending" or payment.status not in {"APPROVAL_PENDING", "CAPTURE_PENDING"}:
        raise_recovery_required()


def validate_new_payment_integrity(payment: Payment, transfer: Transfer) -> None:
    """Reject newly persisted payments that do not match their transfer snapshot."""
    if payment.transfer_id != transfer.id:
        raise_recovery_required()
    if payment.amount_minor != transfer.amount_minor or payment.currency != transfer.currency:
        raise_recovery_required()
    if transfer.status != "pending":
        raise_recovery_required()
    if payment.status not in {"APPROVAL_PENDING", "CAPTURE_PENDING"}:
        raise_recovery_required()


def validate_active_replay(
    db: Session,
    previous: IdempotencyKey,
    transfer: Transfer,
    user_id: str,
    operation: str,
    resource_id: str,
    fingerprint: str,
) -> dict[str, Any]:
    if previous.user_id != user_id:
        raise HTTPException(status_code=409, detail="Idempotency-Key belongs to another user")
    if previous.operation != operation or previous.resource_id != resource_id:
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used for another resource or operation")
    if previous.request_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different parameters")

    response = previous.response_json if isinstance(previous.response_json, dict) else {}
    payment_id = response.get("payment_id")
    payment = db.get(Payment, payment_id) if payment_id else None
    validate_payment_integrity(payment, transfer, user_id)
    return response

