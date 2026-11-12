"""PostgreSQL 16.15 recalculation/payment-obligation race tests."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.main import recalculate_settlement
from app.models import (
    CaptureAttempt,
    Expense,
    ExpenseSplit,
    Group,
    GroupMember,
    Payment,
    PaymentAttempt,
    Settlement,
    Transfer,
    User,
)

DATABASE_URL = os.getenv("SETTLEMATE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SETTLEMATE_TEST_POSTGRES_URL is not configured")


def setup_db(*, payment=False, attempt_status=None, capture_status=None):
    engine = sa.create_engine(DATABASE_URL, pool_size=12, max_overflow=8, isolation_level="READ COMMITTED")
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    with Session(engine) as db:
        suffix = uuid4().hex
        debtor = User(email=f"debtor-{suffix}@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email=f"owner-{suffix}@example.com", display_name="Owner", password_hash="hash")
        db.add_all([debtor, owner]); db.flush()
        group = Group(name=f"Group-{suffix}", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([GroupMember(group_id=group.id, user_id=debtor.id), GroupMember(group_id=group.id, user_id=owner.id, role="owner")])
        expense = Expense(group_id=group.id, paid_by_id=owner.id, description="expense", amount_minor=100, currency="INR", status="active"); db.add(expense); db.flush()
        db.add(ExpenseSplit(expense_id=expense.id, user_id=debtor.id, amount_minor=100));
        settlement = Settlement(group_id=group.id, status="recalculated", currency="INR"); db.add(settlement); db.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=100, currency="INR", status="pending"); db.add(transfer); db.flush()
        parent = None
        payment_row = None
        if payment:
            payment_row = Payment(transfer_id=transfer.id, provider_order_id="order-recalc", amount_minor=100, currency="INR", status="APPROVAL_PENDING"); db.add(payment_row); db.flush()
            parent = PaymentAttempt(transfer_id=transfer.id, user_id=debtor.id, operation="payment-order", request_fingerprint="fp", amount_minor=100, currency="INR", provider="paypal", provider_request_id=f"request-{suffix}", provider_order_id="order-recalc", status=attempt_status or "COMPLETED"); db.add(parent); db.flush()
            if capture_status:
                db.add(CaptureAttempt(payment_id=payment_row.id, transfer_id=transfer.id, parent_attempt_id=parent.id, provider="paypal", provider_order_id="order-recalc", provider_request_id=f"capture-{suffix}", amount_minor=100, currency="INR", status=capture_status, reconciliation_state="RESOLVED" if capture_status == "METADATA_PERSISTED" else "REQUIRED", provider_capture_id="capture-known" if capture_status == "METADATA_PERSISTED" else None, provider_capture_status="COMPLETED" if capture_status == "METADATA_PERSISTED" else None))
        db.commit()
        return engine, settlement.id, transfer.id, debtor.id, payment_row.id if payment_row else None


def recalc_worker(settlement_id, user_id):
    engine = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
    try:
        with Session(engine) as db:
            return recalculate_settlement(settlement_id, db.get(User, user_id), db)
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__
    finally:
        engine.dispose()


@pytest.mark.parametrize("attempt_status", ["PROVIDER_REQUESTED", "PERSISTENCE_PENDING", "RECONCILIATION_REQUIRED"])
def test_recalculation_never_supersedes_provider_order_attempt(attempt_status):
    engine, settlement_id, transfer_id, user_id, _ = setup_db(payment=True, attempt_status=attempt_status)
    result = recalc_worker(settlement_id, user_id)
    assert result == "HTTPException"
    with Session(engine) as db:
        assert db.get(Transfer, transfer_id).status == "pending"
    engine.dispose()


def test_recalculation_blocks_metadata_persisted_capture():
    engine, settlement_id, transfer_id, user_id, _ = setup_db(payment=True, attempt_status="COMPLETED", capture_status="METADATA_PERSISTED")
    assert recalc_worker(settlement_id, user_id) == "HTTPException"
    with Session(engine) as db:
        assert db.get(Transfer, transfer_id).status == "pending"
    engine.dispose()


def test_concurrent_recalculations_do_not_create_duplicate_replacements():
    engine, settlement_id, _transfer_id, user_id, _ = setup_db()
    barrier = Barrier(2)

    def worker():
        barrier.wait()
        return recalc_worker(settlement_id, user_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert all(isinstance(result, dict) for result in results)
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Transfer).where(Transfer.settlement_id == settlement_id, Transfer.status == "pending")) == 1
    engine.dispose()


def test_unpaid_transfer_can_be_superseded_safely():
    engine, settlement_id, transfer_id, user_id, _ = setup_db()
    with Session(engine) as db:
        transfer = db.get(Transfer, transfer_id)
        transfer.amount_minor = 99
        db.commit()
    result = recalc_worker(settlement_id, user_id)
    assert isinstance(result, dict)
    with Session(engine) as db:
        assert db.get(Transfer, transfer_id).status == "superseded"
        assert db.scalar(sa.select(sa.func.count()).select_from(Transfer).where(Transfer.settlement_id == settlement_id, Transfer.status == "pending")) == 1
    engine.dispose()


def test_recalculation_serializes_with_payment_creation_lock():
    engine, settlement_id, transfer_id, user_id, _ = setup_db()
    barrier = Barrier(2)

    def locked_recalc():
        local = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
        try:
            with Session(local) as db:
                barrier.wait()
                return recalculate_settlement(settlement_id, db.get(User, user_id), db)
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        finally:
            local.dispose()

    def payment_lock():
        local = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")
        try:
            with Session(local) as db:
                barrier.wait()
                transfer = db.scalar(sa.select(Transfer).where(Transfer.id == transfer_id).with_for_update())
                transfer.status = "pending"
                db.commit()
                return "locked"
        finally:
            local.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(locked_recalc), pool.submit(payment_lock)]]
    assert any(result == "locked" for result in results)
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Transfer).where(Transfer.settlement_id == settlement_id, Transfer.status == "pending")) <= 1
    engine.dispose()
