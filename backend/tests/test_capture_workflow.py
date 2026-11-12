"""Focused durable capture workflow tests using mocked PayPal calls."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import Base
from app.main import capture_payment
from app.models import (
    CaptureAttempt,
    Group,
    GroupMember,
    Payment,
    PaymentAttempt,
    Settlement,
    Transfer,
    User,
)
from app.services.capture_attempts import reconcile_capture


def fixture_db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'capture.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        debtor = User(email="debtor@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email="owner@example.com", display_name="Owner", password_hash="hash")
        db.add_all([debtor, owner]); db.flush()
        group = Group(name="Group", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([GroupMember(group_id=group.id, user_id=debtor.id), GroupMember(group_id=group.id, user_id=owner.id, role="owner")])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR"); db.add(settlement); db.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=100, currency="INR", status="pending")
        db.add(transfer); db.flush()
        payment = Payment(transfer_id=transfer.id, provider_order_id="order-1", amount_minor=100, currency="INR", status="APPROVAL_PENDING")
        db.add(payment); db.flush()
        db.add(PaymentAttempt(transfer_id=transfer.id, user_id=debtor.id, operation="payment-order", request_fingerprint="fp", amount_minor=100, currency="INR", provider="paypal", provider_request_id="order-request", provider_order_id="order-1", status="COMPLETED"))
        db.commit()
        ids = SimpleNamespace(user=debtor.id, payment=payment.id)
    return engine, ids


def test_capture_reserves_before_provider_and_persists_metadata_then_payment(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    seen = {}

    def provider(_client, order_id, request_id=None):
        with Session(engine) as independent:
            attempt = independent.scalar(sa.select(CaptureAttempt))
            seen["status"] = attempt.status
            seen["request_id"] = request_id
        return {"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "capture-1", "status": "COMPLETED", "amount": {"currency_code": "INR", "value": "1.00"}}]}}]}

    monkeypatch.setattr("app.main.PayPalClient.capture_order", provider)
    with Session(engine) as db:
        result = capture_payment(ids.payment, db.get(User, ids.user), db)
    assert result["status"] == "COMPLETED"
    assert seen["status"] == "PROVIDER_REQUESTED"
    assert seen["request_id"]
    with Session(engine) as db:
        assert db.get(Payment, ids.payment).provider_capture_id == "capture-1"
        assert db.scalar(sa.select(CaptureAttempt)).status == "COMPLETED"
    engine.dispose()


def test_timeout_blocks_replacement_and_reuses_no_new_request(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    calls = []
    monkeypatch.setattr("app.main.PayPalClient.capture_order", lambda _client, order_id, request_id=None: calls.append(request_id) or (_ for _ in ()).throw(TimeoutError()))
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        capture_payment(ids.payment, db.get(User, ids.user), db)
    assert error.value.status_code == 503
    with Session(engine) as db:
        attempt = db.scalar(sa.select(CaptureAttempt))
        assert attempt.status == "RECONCILIATION_REQUIRED"
        request_id = attempt.provider_request_id
    with Session(engine) as db, pytest.raises(HTTPException):
        capture_payment(ids.payment, db.get(User, ids.user), db)
    assert calls == [request_id]
    engine.dispose()


def test_mismatched_provider_capture_fails_closed(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    monkeypatch.setattr("app.main.PayPalClient.capture_order", lambda *args, **kwargs: {"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "capture-1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "1.00"}}]}}]})
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        capture_payment(ids.payment, db.get(User, ids.user), db)
    assert error.value.status_code == 409
    with Session(engine) as db:
        assert db.get(Payment, ids.payment).provider_capture_id is None
        assert db.scalar(sa.select(CaptureAttempt)).status == "RECONCILIATION_REQUIRED"
    engine.dispose()


def test_metadata_persisted_recovery_completes_without_provider_call(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        payment = db.get(Payment, ids.payment)
        db.add(CaptureAttempt(payment_id=payment.id, transfer_id=payment.transfer_id, parent_attempt_id=db.scalar(sa.select(PaymentAttempt)).id, provider="paypal", provider_order_id="order-1", provider_request_id="capture-request", provider_capture_id="capture-1", amount_minor=100, currency="INR", status="METADATA_PERSISTED", reconciliation_state="NOT_REQUIRED", provider_capture_status="COMPLETED"))
        db.commit()
    monkeypatch.setattr("app.main.PayPalClient.capture_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
    with Session(engine) as db:
        result = capture_payment(ids.payment, db.get(User, ids.user), db)
    assert result["status"] == "COMPLETED"
    engine.dispose()


def test_reconciliation_reads_provider_and_completes_without_capture_call(tmp_path):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        payment = db.get(Payment, ids.payment)
        attempt = CaptureAttempt(payment_id=payment.id, transfer_id=payment.transfer_id, parent_attempt_id=db.scalar(sa.select(PaymentAttempt)).id, provider="paypal", provider_order_id="order-1", provider_request_id="capture-request", amount_minor=100, currency="INR", status="RECONCILIATION_REQUIRED", reconciliation_state="REQUIRED")
        db.add(attempt); db.commit(); attempt_id = attempt.id
    reads = []
    with Session(engine) as db:
        payment, attempt = reconcile_capture(db, attempt_id, lambda order_id: reads.append(order_id) or {"id": "order-1", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "capture-2", "status": "COMPLETED", "amount": {"currency_code": "INR", "value": "1.00"}}]}}]})
        assert payment.provider_capture_id == "capture-2"
        assert attempt.status == "COMPLETED"
    assert reads == ["order-1"]
    engine.dispose()


def test_unverifiable_reconciliation_is_blocked(tmp_path):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        payment = db.get(Payment, ids.payment)
        attempt = CaptureAttempt(payment_id=payment.id, transfer_id=payment.transfer_id, parent_attempt_id=db.scalar(sa.select(PaymentAttempt)).id, provider="paypal", provider_order_id="order-1", provider_request_id="capture-request", amount_minor=100, currency="INR", status="RECONCILIATION_REQUIRED", reconciliation_state="REQUIRED")
        db.add(attempt); db.commit(); attempt_id = attempt.id
    with Session(engine) as db, pytest.raises(RuntimeError):
        reconcile_capture(db, attempt_id, lambda _order_id: (_ for _ in ()).throw(TimeoutError()))
    with Session(engine) as db:
        assert db.get(CaptureAttempt, attempt_id).reconciliation_state == "BLOCKED"
        assert db.get(Payment, ids.payment).provider_capture_id is None
    engine.dispose()
