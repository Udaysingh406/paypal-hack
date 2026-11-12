import hashlib
import json
from datetime import datetime
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
    IdempotencyKey,
    Payment,
    Settlement,
    Transfer,
    User,
)


class Request:
    url = "http://test/api/v1/transfers/transfer/payment-order"


def make_fixture(tmp_path, *, archive=False, payment=True, payment_amount=100, transfer_amount=100, payment_transfer=None):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'idempotency.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        debtor = User(email="debtor@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email="owner@example.com", display_name="Owner", password_hash="hash")
        db.add_all([debtor, owner]); db.flush()
        group = Group(name="Group", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([GroupMember(group_id=group.id, user_id=debtor.id), GroupMember(group_id=group.id, user_id=owner.id, role="owner")])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR"); db.add(settlement); db.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=transfer_amount, currency="INR")
        db.add(transfer); db.flush()
        payment_row = None
        if payment:
            payment_row = Payment(transfer_id=payment_transfer or transfer.id, amount_minor=payment_amount, currency="INR", status="APPROVAL_PENDING", provider_order_id="order")
            db.add(payment_row); db.flush()
        db.commit()
        ids = SimpleNamespace(debtor=debtor.id, transfer=transfer.id, payment=payment_row.id if payment_row else None)
    if archive:
        with engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TABLE idempotency_key_archive (
                original_id varchar(36) PRIMARY KEY, key varchar(160) NOT NULL,
                user_id varchar(36) NOT NULL, operation varchar(64) NOT NULL,
                resource_id varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL,
                response_json json NOT NULL, original_created_at timestamp NOT NULL,
                original_updated_at timestamp NOT NULL, archive_reason varchar(240) NOT NULL,
                archived_at timestamp NOT NULL, financial_integrity_classification varchar(32) NOT NULL)""")
            connection.execute(sa.text("""INSERT INTO idempotency_key_archive VALUES
                (:original, :key, :user, 'payment-order', :transfer, '{}',
                 '{\"payment_id\":\"hidden\",\"order_id\":\"hidden\"}', :now, :now,
                 'duplicate active key; provider status unverified', '2025-01-01', 'consistent')"""), [
                     {"original": f"archive-{i}", "key": f"historical-key-{i}", "user": ids.debtor, "transfer": ids.transfer, "now": datetime(2025, 1, 1)}
                     for i in range(5)
                 ])
    return engine, ids


@pytest.mark.parametrize("archive_key", [f"historical-key-{i}" for i in range(5)])
def test_archived_key_is_generic_recovery_conflict_and_never_calls_provider(tmp_path, monkeypatch, archive_key):
    engine, ids = make_fixture(tmp_path, archive=True)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        create_payment_order(ids.transfer, Request(), db.get(User, ids.debtor), db, archive_key)
    assert error.value.status_code == 409
    assert error.value.detail == "Payment recovery required"
    engine.dispose()


def test_active_replay_requires_exact_integrity_and_scope(tmp_path, monkeypatch):
    engine, ids = make_fixture(tmp_path, payment=False)
    with Session(engine) as db:
        user = db.get(User, ids.debtor)
        key = IdempotencyKey(key="active-key", user_id=user.id, operation="payment-order", resource_id=ids.transfer, request_fingerprint="wrong", response_json={"payment_id": ids.payment})
        db.add(key); db.commit()
        monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
        with pytest.raises(HTTPException) as error:
            create_payment_order(ids.transfer, Request(), user, db, "active-key")
        assert error.value.status_code == 409
    engine.dispose()


def test_exact_active_replay_returns_existing_response(tmp_path, monkeypatch):
    engine, ids = make_fixture(tmp_path)
    with Session(engine) as db:
        user = db.get(User, ids.debtor)
        fingerprint = hashlib.sha256(json.dumps({
            "transfer_id": ids.transfer,
            "amount_minor": 100,
            "currency": "INR",
            "return_url": Request.url + "/return",
            "cancel_url": Request.url + "/cancel",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.add(IdempotencyKey(
            key="active-key", user_id=user.id, operation="payment-order",
            resource_id=ids.transfer, request_fingerprint=fingerprint,
            response_json={"payment_id": ids.payment, "status": "APPROVAL_PENDING"},
        ))
        db.commit()
        monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
        assert create_payment_order(ids.transfer, Request(), user, db, "active-key")["payment_id"] == ids.payment
    engine.dispose()


def test_existing_payment_without_idempotency_is_recovery_required(tmp_path, monkeypatch):
    engine, ids = make_fixture(tmp_path)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
    with Session(engine) as db:
        with pytest.raises(HTTPException) as error:
            create_payment_order(ids.transfer, Request(), db.get(User, ids.debtor), db, "new-key")
        assert error.value.detail == "Payment recovery required"
    engine.dispose()


def test_missing_archive_table_fails_closed_before_provider(tmp_path, monkeypatch):
    engine, ids = make_fixture(tmp_path, payment=False)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
    with Session(engine) as db:
        with pytest.raises(HTTPException) as error:
            create_payment_order(ids.transfer, Request(), db.get(User, ids.debtor), db, "new-key")
        assert error.value.status_code == 503
        assert error.value.detail == "Payment recovery is unavailable"
    engine.dispose()


def test_archive_query_failure_fails_closed_before_provider(tmp_path, monkeypatch):
    engine, ids = make_fixture(tmp_path, archive=True, payment=False)
    monkeypatch.setattr("app.services.idempotency._archive_table", lambda db: (_ for _ in ()).throw(sa.exc.OperationalError("archive", {}, RuntimeError("unavailable"))))
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
    with Session(engine) as db:
        with pytest.raises(HTTPException) as error:
            create_payment_order(ids.transfer, Request(), db.get(User, ids.debtor), db, "new-key")
        assert error.value.status_code == 503
    engine.dispose()


def test_amount_mismatch_is_recovery_required(tmp_path, monkeypatch):
    engine, ids = make_fixture(tmp_path, payment_amount=99)
    monkeypatch.setattr("app.main.PayPalClient.create_order", lambda *args, **kwargs: pytest.fail("provider must not be called"))
    with Session(engine) as db:
        with pytest.raises(HTTPException) as error:
            create_payment_order(ids.transfer, Request(), db.get(User, ids.debtor), db, "new-key")
        assert error.value.detail == "Payment recovery required"
    engine.dispose()
