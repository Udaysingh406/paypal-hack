"""Read-only audit of the five conflicting payment orders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from app.core.config import get_settings
from app.db import SessionLocal
from app.models import IdempotencyKey, Payment, Transfer
from app.services.payments import PayPalClient
from sqlalchemy import select


@dataclass(frozen=True)
class AuditTarget:
    ordinal: int
    order_id: str
    local_payment_status: str
    local_transfer_status: str
    amount_minor: int
    currency: str
    local_integrity: str


@dataclass(frozen=True)
class AuditResult:
    ordinal: int
    local_payment_status: str
    local_transfer_status: str
    provider_status: str
    capture_status: str
    classification: str
    amount_match: bool | None
    currency_match: bool | None
    local_integrity: str


def load_conflicting_targets() -> list[AuditTarget]:
    """Read only the conflict metadata, then close the database session."""
    with SessionLocal() as db:
        rows = db.scalars(
            select(IdempotencyKey)
            .order_by(IdempotencyKey.created_at, IdempotencyKey.id)
        ).all()
        by_key: dict[str, list[IdempotencyKey]] = {}
        for row in rows:
            by_key.setdefault(row.key, []).append(row)
        conflicts = [row for group in by_key.values() if len(group) > 1 for row in group]
        conflicts.sort(key=lambda row: (row.created_at, row.id))
        if len(conflicts) != 5:
            raise RuntimeError("Expected exactly five conflicting idempotency records")

        targets: list[AuditTarget] = []
        for ordinal, row in enumerate(conflicts, start=1):
            response = row.response_json if isinstance(row.response_json, dict) else {}
            payment_id = response.get("payment_id")
            payment = db.get(Payment, payment_id) if payment_id else None
            transfer = db.get(Transfer, payment.transfer_id) if payment else None
            if not payment or not transfer or payment.provider_order_id is None:
                raise RuntimeError(f"Conflicting record {ordinal} has no auditable provider order")
            association_match = payment.transfer_id == row.resource_id and transfer.from_user_id == row.user_id
            terms_match = payment.amount_minor == transfer.amount_minor and payment.currency == transfer.currency
            targets.append(AuditTarget(
                ordinal=ordinal,
                order_id=payment.provider_order_id,
                local_payment_status=payment.status,
                local_transfer_status=transfer.status,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
                local_integrity="verified" if association_match and terms_match else "local_mismatch",
            ))
    return targets


def _provider_terms(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    purchase_units = payload.get("purchase_units")
    if not isinstance(purchase_units, list) or not purchase_units:
        return None, None
    amount = purchase_units[0].get("amount")
    if not isinstance(amount, dict):
        return None, None
    value = amount.get("value")
    currency = amount.get("currency_code")
    try:
        amount_minor = round(float(value) * 100) if value is not None else None
    except (TypeError, ValueError):
        amount_minor = None
    return amount_minor, currency if isinstance(currency, str) else None


def _capture_status(payload: dict[str, Any]) -> str:
    purchase_units = payload.get("purchase_units")
    if not isinstance(purchase_units, list) or not purchase_units:
        return "unknown"
    payments = purchase_units[0].get("payments")
    if not isinstance(payments, dict):
        return "none"
    captures = payments.get("captures")
    if not isinstance(captures, list) or not captures:
        return "none"
    statuses = [capture.get("status") for capture in captures if isinstance(capture, dict)]
    return ",".join(sorted({status for status in statuses if isinstance(status, str)})) or "unknown"


def audit_target(target: AuditTarget, get_order: Callable[[str], dict[str, Any]]) -> AuditResult:
    try:
        payload = get_order(target.order_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, "not_found", "unknown", "not_found", None, None, target.local_integrity)
        return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, "http_error", "unknown", "unavailable", None, None, target.local_integrity)
    except httpx.TimeoutException:
        return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, "timeout", "unknown", "unavailable", None, None, target.local_integrity)
    except RuntimeError:
        return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, "credentials_unavailable", "unknown", "unavailable", None, None, target.local_integrity)
    except (httpx.RequestError, ValueError, TypeError):
        return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, "provider_error", "unknown", "unavailable", None, None, target.local_integrity)

    if not isinstance(payload, dict):
        return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, "malformed", "unknown", "malformed", None, None, target.local_integrity)
    provider_amount, provider_currency = _provider_terms(payload)
    amount_match = provider_amount == target.amount_minor if provider_amount is not None else False
    currency_match = provider_currency.upper() == target.currency.upper() if provider_currency else False
    if not amount_match or not currency_match:
        return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, str(payload.get("status", "unknown")), _capture_status(payload), "mismatch", amount_match, currency_match, target.local_integrity)
    classification = "verified_terms" if target.local_integrity == "verified" else "local_mismatch"
    return AuditResult(target.ordinal, target.local_payment_status, target.local_transfer_status, str(payload.get("status", "unknown")), _capture_status(payload), classification, True, True, target.local_integrity)


def run_audit() -> list[AuditResult]:
    targets = load_conflicting_targets()
    client = PayPalClient(get_settings())
    return [audit_target(target, client.get_order) for target in targets]


def main() -> int:
    results = run_audit()
    for result in results:
        print({
            "record": result.ordinal,
            "local_payment_status": result.local_payment_status,
            "local_transfer_status": result.local_transfer_status,
            "provider_status": result.provider_status,
            "capture_status": result.capture_status,
            "classification": result.classification,
            "amount_match": result.amount_match,
            "currency_match": result.currency_match,
            "local_integrity": result.local_integrity,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
