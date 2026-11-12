"""PostgreSQL-only race tests for durable capture state."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.main import capture_payment, reconcile_payment_capture
from app.models import (
    CaptureAttempt,
    Group,
    GroupMember,
    IdempotencyKey,
    Payment,
    PaymentAttempt,
    Settlement,
    Transfer,
    User,
)
from app.services.capture_attempts import complete_metadata_capture, reserve_capture

DATABASE_URL = os.getenv("SETTLEMATE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SETTLEMATE_TEST_POSTGRES_URL is not configured")


def setup_db():
    engine = sa.create_engine(DATABASE_URL, pool_size=10, max_overflow=5, isolation_level="READ COMMITTED")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(email="debtor-race@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email="owner-race@example.com", display_name="Owner", password_hash="hash")
        db.add_all([user, owner]); db.flush()
        group = Group(name="Race Group", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([GroupMember(group_id=group.id, user_id=user.id), GroupMember(group_id=group.id, user_id=owner.id, role="owner")])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR"); db.add(settlement); db.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=user.id, to_user_id=owner.id, amount_minor=100, currency="INR", status="pending"); db.add(transfer); db.flush()
        payment = Payment(transfer_id=transfer.id, provider_order_id="order-race", amount_minor=100, currency="INR", status="APPROVAL_PENDING"); db.add(payment); db.flush()
        parent = PaymentAttempt(transfer_id=transfer.id, user_id=user.id, operation="payment-order", request_fingerprint="fp", amount_minor=100, currency="INR", provider="paypal", provider_request_id="order-request-race", provider_order_id="order-race", status="COMPLETED"); db.add(parent); db.commit()
        return engine, payment.id, transfer.id, user.id, parent.id


def reserve_worker(url, payment_id, transfer_id):
    engine = sa.create_engine(url, isolation_level="READ COMMITTED")
    try:
        with Session(engine) as db:
            payment = db.get(Payment, payment_id)
            transfer = db.get(Transfer, transfer_id)
            return reserve_capture(db, payment, transfer).id
    except (IntegrityError, RuntimeError) as exc:
        return type(exc).__name__
    finally:
        engine.dispose()


def test_simultaneous_reservations_allow_one_active_attempt():
    engine, payment_id, transfer_id, _, _ = setup_db()
    barrier = Barrier(2)
    def worker():
        barrier.wait()
        return reserve_worker(DATABASE_URL, payment_id, transfer_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CaptureAttempt)) == 1
    assert len([x for x in results if isinstance(x, str) and len(x) > 20]) == 1
    engine.dispose()


def test_same_idempotency_key_concurrently_has_one_row():
    engine, _, _, user_id, _ = setup_db()
    barrier = Barrier(2)

    def worker():
        barrier.wait()
        local = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
        try:
            with Session(local) as db:
                db.add(IdempotencyKey(key="same-race-key", user_id=user_id, operation="payment-order", resource_id="same-resource", request_fingerprint="fp", response_json={}))
                db.commit(); return "ok"
        except IntegrityError:
            return "conflict"
        finally:
            local.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(results) == ["conflict", "ok"]
    engine.dispose()


def test_concurrent_completion_yields_one_completed_capture():
    engine, payment_id, _, _, parent_id = setup_db()
    with Session(engine) as db:
        payment = db.get(Payment, payment_id)
        db.add(CaptureAttempt(payment_id=payment_id, transfer_id=payment.transfer_id, parent_attempt_id=parent_id, provider="paypal", provider_order_id="order-race", provider_request_id="capture-race", provider_capture_id="capture-race", amount_minor=100, currency="INR", status="METADATA_PERSISTED", reconciliation_state="NOT_REQUIRED", provider_capture_status="COMPLETED")); db.commit()
    attempt_id = None
    with Session(engine) as db:
        attempt_id = db.scalar(sa.select(CaptureAttempt.id))
    barrier = Barrier(2)

    def worker():
        local = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
        try:
            with Session(local) as db:
                barrier.wait(); complete_metadata_capture(db, attempt_id); return "ok"
        except RuntimeError:
            return "conflict"
        finally:
            local.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(results) == ["conflict", "ok"]
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CaptureAttempt).where(CaptureAttempt.status == "COMPLETED")) == 1
        assert db.get(Payment, payment_id).provider_capture_id == "capture-race"
    engine.dispose()


def test_completed_payment_cannot_receive_another_capture():
    engine, payment_id, _, _, _ = setup_db()
    with Session(engine) as db:
        payment = db.get(Payment, payment_id); payment.status = "COMPLETED"; payment.provider_capture_id = "capture-existing"; db.commit()
    with Session(engine) as db, pytest.raises(RuntimeError):
        reserve_capture(db, db.get(Payment, payment_id), db.get(Transfer, db.get(Payment, payment_id).transfer_id))
    engine.dispose()


def test_different_keys_targeting_one_payment_still_share_one_active_slot():
    engine, payment_id, transfer_id, _, _ = setup_db()
    with Session(engine) as db:
        first = reserve_capture(db, db.get(Payment, payment_id), db.get(Transfer, transfer_id))
        first_id = first.id
    with Session(engine) as db:
        with pytest.raises(RuntimeError):
            reserve_capture(db, db.get(Payment, payment_id), db.get(Transfer, transfer_id))
        assert db.get(CaptureAttempt, first_id).status == "RESERVED"
    engine.dispose()


def _completed_order():
    return {"id": "order-race", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "capture-race", "status": "COMPLETED", "amount": {"currency_code": "INR", "value": "1.00"}}]}}]}


def test_concurrent_endpoint_capture_requests_make_one_provider_call(monkeypatch):
    engine, payment_id, _, user_id, _ = setup_db()
    start_barrier = Barrier(2)
    calls = []

    def capture(_client, order_id, request_id=None):
        calls.append(request_id)
        return _completed_order()

    monkeypatch.setattr("app.main.PayPalClient.capture_order", capture)

    def worker():
        local = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
        try:
            start_barrier.wait()
            with Session(local) as db:
                return capture_payment(payment_id, db.get(User, user_id), db)
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        finally:
            local.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert len(calls) == 1
    assert sum(isinstance(result, dict) and result["status"] == "COMPLETED" for result in results) == 2
    engine.dispose()


def test_timeout_racing_reconciliation_blocks_or_completes_without_capture_retry(monkeypatch):
    engine, payment_id, _, user_id, parent_id = setup_db()
    with Session(engine) as db:
        payment = db.get(Payment, payment_id)
        attempt = CaptureAttempt(payment_id=payment_id, transfer_id=payment.transfer_id, parent_attempt_id=parent_id, provider="paypal", provider_order_id="order-race", provider_request_id="capture-timeout", amount_minor=100, currency="INR", status="RECONCILIATION_REQUIRED", reconciliation_state="REQUIRED")
        db.add(attempt); db.commit()
    reads = []
    start_barrier = Barrier(2)

    def get_order(_client, order_id):
        reads.append(order_id)
        return _completed_order()

    monkeypatch.setattr("app.main.PayPalClient.get_order", get_order)

    def worker():
        local = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
        try:
            start_barrier.wait()
            with Session(local) as db:
                return reconcile_payment_capture(payment_id, db.get(User, user_id), db)
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        finally:
            local.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert len(reads) == 2
    assert sum(isinstance(result, dict) and result["status"] == "COMPLETED" for result in results) == 1
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CaptureAttempt).where(CaptureAttempt.status == "COMPLETED")) == 1
    engine.dispose()
