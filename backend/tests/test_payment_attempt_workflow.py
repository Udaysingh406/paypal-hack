"""Task 3B.2 regression tests for the durable payment-attempt workflow."""

import hashlib
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import Base
from app.main import create_payment_order
from app.models import (
    Group,
    GroupMember,
    Payment,
    PaymentAttempt,
    Settlement,
    Transfer,
    User,
)


class Request:
    url = "http://test/api/v1/transfers/transfer/payment-order"


VALID_ORDER = {"id": "order-1", "purchase_units": [{"amount": {"currency_code": "INR", "value": "1.00"}}], "links": []}


def fixture_db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'attempt-workflow.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE idempotency_key_archive (
            original_id varchar(36) PRIMARY KEY, key varchar(160) NOT NULL,
            user_id varchar(36) NOT NULL, operation varchar(64) NOT NULL,
            resource_id varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL,
            response_json json NOT NULL, original_created_at datetime NOT NULL,
            original_updated_at datetime NOT NULL, archive_reason varchar(240) NOT NULL,
            archived_at datetime NOT NULL, financial_integrity_classification varchar(32) NOT NULL)""")
    with Session(engine) as db:
        debtor = User(email="debtor@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email="owner@example.com", display_name="Owner", password_hash="hash")
        db.add_all([debtor, owner]); db.flush()
        group = Group(name="Group", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([
            GroupMember(group_id=group.id, user_id=debtor.id),
            GroupMember(group_id=group.id, user_id=owner.id, role="owner"),
        ])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR")
        db.add(settlement); db.flush()
        transfer = Transfer(
            settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id,
            amount_minor=100, currency="INR", status="pending",
        )
        db.add(transfer); db.commit()
        ids = SimpleNamespace(user=debtor.id, transfer=transfer.id)
    return engine, ids


def call(engine, ids, key="payment-key"):
    with Session(engine) as db:
        return create_payment_order(ids.transfer, Request(), db.get(User, ids.user), db, key)


def test_reservation_is_committed_before_provider_call_and_provider_has_no_open_transaction(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    observed = {}

    def provider(*args, **kwargs):
        with Session(engine) as independent_db:
            observed["attempt"] = independent_db.scalar(sa.select(PaymentAttempt))
        return VALID_ORDER

    monkeypatch.setattr("app.main.PayPalClient.create_order", provider)
    call(engine, ids)
    assert observed["attempt"].status == "PROVIDER_REQUESTED"
    assert observed["attempt"].provider_request_id
    engine.dispose()


def test_provider_request_id_is_reused_without_a_second_provider_call(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    requests = []
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *a, **k: requests.append(k["request_id"]) or VALID_ORDER)
    call(engine, ids)
    call(engine, ids)
    assert len(requests) == 1 and requests[0]
    with Session(engine) as db:
        attempts = db.scalars(sa.select(PaymentAttempt)).all()
    assert len(attempts) == 1
    engine.dispose()


@pytest.mark.parametrize(
    "provider_error,classification",
    [(TimeoutError(), "timeout"), (ConnectionError(), "provider_uncertain")],
)
def test_provider_failure_leaves_recoverable_attempt(tmp_path, monkeypatch, provider_error, classification):
    engine, ids = fixture_db(tmp_path)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *a, **k: (_ for _ in ()).throw(provider_error))
    with pytest.raises((HTTPException, TimeoutError, ConnectionError)):
        call(engine, ids)
    with Session(engine) as db:
        attempt = db.scalar(sa.select(PaymentAttempt))
    assert attempt is not None
    assert attempt.status in {"FAILED", "RECONCILIATION_REQUIRED"}
    assert attempt.error_classification == classification
    engine.dispose()


@pytest.mark.parametrize("order", [{}, {"id": ""}, {"id": "other-order", "amount": 999}, {"id": "order-2", "purchase_units": [{"amount": {"currency_code": "INR", "value": "1.009"}}]}])
def test_malformed_or_mismatched_provider_response_requires_recovery(tmp_path, monkeypatch, order):
    engine, ids = fixture_db(tmp_path)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *a, **k: order)
    with pytest.raises(HTTPException) as error:
        call(engine, ids)
    assert error.value.status_code == 409
    assert error.value.detail == "Payment recovery required"
    with Session(engine) as db:
        assert db.scalar(sa.select(Payment)) is None
        assert db.scalar(sa.select(PaymentAttempt)) is not None
    engine.dispose()


def test_persistence_failure_marks_attempt_pending_and_preserves_payment_safety(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *a, **k: VALID_ORDER)
    original_commit = Session.commit
    calls = 0

    def fail_once(session):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise sa.exc.OperationalError("commit", {}, RuntimeError("db down"))
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_once)
    with pytest.raises(HTTPException) as error:
        call(engine, ids)
    assert error.value.status_code == 503
    with Session(engine) as db:
        attempt = db.scalar(sa.select(PaymentAttempt))
    assert attempt is not None
    assert attempt.status in {"PERSISTENCE_PENDING", "RECONCILIATION_REQUIRED"}
    engine.dispose()


def test_reservation_uniqueness_race_returns_recovery_without_provider_call(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        transfer = db.get(Transfer, ids.transfer)
        winner = PaymentAttempt(
            transfer_id=transfer.id, user_id=ids.user, idempotency_key="winning-key",
            operation="payment-order", request_fingerprint="different-fingerprint",
            amount_minor=transfer.amount_minor, currency=transfer.currency,
            return_url="http://winner/return", cancel_url="http://winner/cancel",
            provider_request_id="winner-request", status="RESERVED",
        )
        db.add(winner); db.commit()

    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(
        "app.main.reserve_payment_order",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            IntegrityError("insert", {}, RuntimeError(
                "UNIQUE constraint failed: payment_attempts.transfer_id, payment_attempts.operation"
            ))
        ),
    )
    provider_calls = []
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: provider_calls.append(True))
    with pytest.raises(HTTPException) as error:
        call(engine, ids, key="losing-key")
    assert error.value.status_code == 409
    assert error.value.detail == "Payment attempt conflict"
    assert not provider_calls
    engine.dispose()


def test_reservation_race_with_same_authorized_operation_requires_recovery(tmp_path, monkeypatch):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        transfer = db.get(Transfer, ids.transfer)
        fingerprint = hashlib.sha256(json.dumps({
            "transfer_id": transfer.id,
            "amount_minor": transfer.amount_minor,
            "currency": transfer.currency,
            "return_url": "http://test/api/v1/transfers/transfer/payment-order/return",
            "cancel_url": "http://test/api/v1/transfers/transfer/payment-order/cancel",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        winner = PaymentAttempt(
            transfer_id=transfer.id, user_id=ids.user, idempotency_key="same-key",
            operation="payment-order", request_fingerprint=fingerprint,
            amount_minor=transfer.amount_minor, currency=transfer.currency,
            return_url="http://test/api/v1/transfers/transfer/payment-order/return",
            cancel_url="http://test/api/v1/transfers/transfer/payment-order/cancel",
            provider_request_id="winner-request", status="PROVIDER_REQUESTED",
        )
        db.add(winner); db.commit()
    from sqlalchemy.exc import IntegrityError
    monkeypatch.setattr(
        "app.main.reserve_payment_order",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            IntegrityError("insert", {}, RuntimeError(
                "UNIQUE constraint failed: payment_attempts.transfer_id, payment_attempts.operation"
            ))
        ),
    )
    provider_calls = []
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: provider_calls.append(True))
    with pytest.raises(HTTPException) as error:
        call(engine, ids, key="same-key")
    assert error.value.status_code == 409
    assert error.value.detail == "Payment recovery required"
    assert not provider_calls
    engine.dispose()

