"""PostgreSQL reservation-race tests for payment attempts."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Group, GroupMember, PaymentAttempt, Settlement, Transfer, User

DATABASE_URL = os.getenv("SETTLEMATE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SETTLEMATE_TEST_POSTGRES_URL is not configured")


def setup_transfer():
    engine = sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED", pool_size=8)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        suffix = uuid4().hex
        debtor = User(email=f"debtor-{suffix}@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email=f"owner-{suffix}@example.com", display_name="Owner", password_hash="hash")
        db.add_all([debtor, owner]); db.flush()
        group = Group(name=f"group-{suffix}", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([GroupMember(group_id=group.id, user_id=debtor.id), GroupMember(group_id=group.id, user_id=owner.id)])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR"); db.add(settlement); db.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=100, currency="INR", status="pending")
        db.add(transfer); db.commit()
        return engine, transfer.id, debtor.id, owner.id


def insert_attempt(url, transfer_id, user_id, key):
    engine = sa.create_engine(url, isolation_level="READ COMMITTED")
    try:
        with Session(engine) as db:
            db.add(PaymentAttempt(transfer_id=transfer_id, user_id=user_id, idempotency_key=key, operation="payment-order", request_fingerprint="f" * 64, amount_minor=100, currency="INR", provider_request_id=f"request-{uuid4().hex}", status="RESERVED"))
            db.commit()
            return "won"
    except IntegrityError:
        return "lost"
    finally:
        engine.dispose()


@pytest.mark.parametrize("keys", [("key-a", "key-a"), ("key-a", "key-b")])
def test_same_transfer_race_allows_one_active_reservation(keys):
    engine, transfer_id, debtor_id, _owner_id = setup_transfer()
    barrier = Barrier(2)

    def worker(key):
        barrier.wait()
        return insert_attempt(DATABASE_URL, transfer_id, debtor_id, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, keys))
    assert sorted(results) == ["lost", "won"]
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(PaymentAttempt)) == 1
    engine.dispose()


def test_conflicting_owner_cannot_create_second_active_attempt():
    engine, transfer_id, debtor_id, owner_id = setup_transfer()
    assert insert_attempt(DATABASE_URL, transfer_id, debtor_id, "owner-safe") == "won"
    assert insert_attempt(DATABASE_URL, transfer_id, owner_id, "other-owner") == "lost"
    with Session(engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(PaymentAttempt)) == 1
    engine.dispose()
