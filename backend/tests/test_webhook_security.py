"""Webhook financial-integrity tests with mocked signature verification."""

import asyncio

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.main import paypal_webhook
from app.models import CaptureAttempt, Payment, PaymentAttempt
from tests.test_capture_workflow import fixture_db


class FakeRequest:
    def __init__(self, body):
        self.headers = {"paypal-transmission-id": "test"}
        self._body = body

    async def json(self):
        return self._body


def event(event_id="event-1", *, order_id="order-1", capture_id="capture-1", amount="1.00", currency="INR", status="COMPLETED"):
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": capture_id,
            "status": status,
            "amount": {"currency_code": currency, "value": amount},
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }


def seed_attempt(engine, ids, *, status="RECONCILIATION_REQUIRED"):
    with Session(engine) as db:
        payment = db.get(Payment, ids.payment)
        parent = db.scalar(sa.select(PaymentAttempt))
        db.add(CaptureAttempt(payment_id=payment.id, transfer_id=payment.transfer_id, parent_attempt_id=parent.id, provider="paypal", provider_order_id="order-1", provider_request_id="capture-request-webhook", amount_minor=100, currency="INR", status=status, reconciliation_state="REQUIRED"))
        db.commit()


def invoke(engine, body):
    with Session(engine) as db:
        return asyncio.run(paypal_webhook(FakeRequest(body), db))


def configure(monkeypatch):
    monkeypatch.setattr("app.main.settings.paypal_webhook_id", "configured")
    monkeypatch.setattr("app.main.PayPalClient.verify_webhook", lambda *args, **kwargs: True)


def test_valid_completed_capture_completes_through_capture_attempt(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path); configure(monkeypatch); seed_attempt(engine, ids)
    result = invoke(engine, event())
    assert result == {"status": "processed"}
    with Session(engine) as db:
        assert db.get(Payment, ids.payment).provider_capture_id == "capture-1"
        assert db.scalar(sa.select(CaptureAttempt)).status == "COMPLETED"
    engine.dispose()


@pytest.mark.parametrize("body", [event(capture_id=""), event(amount="1.01"), event(currency="USD"), event(order_id="other-order"), event(status="PENDING")])
def test_invalid_or_conflicting_capture_event_fails_closed(tmp_path, monkeypatch, body):
    engine, ids = fixture_db(tmp_path); configure(monkeypatch); seed_attempt(engine, ids)
    with pytest.raises(HTTPException) as error:
        invoke(engine, body)
    assert error.value.status_code in {400, 409}
    with Session(engine) as db:
        assert db.get(Payment, ids.payment).provider_capture_id is None
        assert db.scalar(sa.select(CaptureAttempt)).status == "RECONCILIATION_REQUIRED"
    engine.dispose()


def test_unrelated_capture_event_is_rejected(tmp_path, monkeypatch):
    engine, _ids = fixture_db(tmp_path); configure(monkeypatch)
    with pytest.raises(HTTPException) as error:
        invoke(engine, event())
    assert error.value.status_code == 409
    with Session(engine) as db:
        assert db.scalar(sa.select(CaptureAttempt)) is None
    engine.dispose()


def test_duplicate_event_is_idempotent(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path); configure(monkeypatch); seed_attempt(engine, ids)
    body = event()
    assert invoke(engine, body)["status"] == "processed"
    assert invoke(engine, body) == {"status": "duplicate"}
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CaptureAttempt).where(CaptureAttempt.status == "COMPLETED")) == 1
    engine.dispose()


def test_already_completed_payment_conflicting_capture_is_rejected(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path); configure(monkeypatch); seed_attempt(engine, ids)
    with Session(engine) as db:
        payment = db.get(Payment, ids.payment); payment.provider_capture_id = "capture-existing"; payment.status = "COMPLETED"; payment.transfer.status = "completed"; db.commit()
    with pytest.raises(HTTPException) as error:
        invoke(engine, event())
    assert error.value.status_code == 409
    engine.dispose()
