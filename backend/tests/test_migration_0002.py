from datetime import datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.db import Base
from app.main import create_payment_order
from app.models import Group, GroupMember, IdempotencyKey, Payment, Settlement, Transfer, User


OLD_REVISION = import_module("migrations.versions.0001_initial_schema")
MIGRATION = import_module("migrations.versions.0002_scope_idempotency_keys")


def run_revision(engine: Engine, revision: object) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        revision.op = Operations(context)
        revision.upgrade()


def make_engine(tmp_path) -> Engine:
    return sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")


def test_upgrade_empty_database_and_model_shape(tmp_path) -> None:
    engine = make_engine(tmp_path)
    run_revision(engine, OLD_REVISION)
    run_revision(engine, MIGRATION)

    columns = {column["name"]: column for column in inspect(engine).get_columns("idempotency_keys")}
    assert set(columns) == {
        "id", "key", "user_id", "operation", "resource_id",
        "request_fingerprint", "response_json", "created_at", "updated_at",
    }
    assert set(IdempotencyKey.__table__.columns.keys()) == set(columns)
    assert columns["id"]["primary_key"] == 1
    assert any(constraint["column_names"] == ["key"] for constraint in inspect(engine).get_unique_constraints("idempotency_keys"))
    assert inspect(engine).get_foreign_keys("idempotency_keys")[0]["referred_table"] == "users"


def test_upgrade_preserves_rows_and_timestamps(tmp_path) -> None:
    engine = make_engine(tmp_path)
    run_revision(engine, OLD_REVISION)
    created = datetime(2024, 1, 2, 3, 4, 5)
    updated = datetime(2024, 2, 3, 4, 5, 6)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (id, email, display_name, password_hash, is_active) VALUES (:id, :email, :name, :hash, :active)"), {
            "id": "user-1", "email": "user@example.com", "name": "User", "hash": "hash", "active": True,
        })
        connection.execute(sa.text("INSERT INTO idempotency_keys (key, user_id, response_json, created_at, updated_at) VALUES (:key, :user, :response, :created, :updated)"), {
            "key": "old-key", "user": "user-1", "response": '{"payment_id":"payment-1"}', "created": created, "updated": updated,
        })
    run_revision(engine, MIGRATION)

    table = sa.Table("idempotency_keys", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(table)).mappings().one()
    assert row["key"] == "old-key"
    assert row["user_id"] == "user-1"
    assert row["response_json"] == {"payment_id": "payment-1"}
    assert row["operation"] == "legacy"
    assert row["resource_id"] == ""
    assert row["request_fingerprint"] == ""
    assert row["created_at"] == created
    assert row["updated_at"] == updated


def test_populated_downgrade_refuses_before_destructive_ddl(tmp_path) -> None:
    engine = make_engine(tmp_path)
    run_revision(engine, OLD_REVISION)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (id, email, display_name, password_hash, is_active) VALUES ('user-1', 'user@example.com', 'User', 'hash', 1)"))
        connection.execute(sa.text("INSERT INTO idempotency_keys (key, user_id, response_json) VALUES ('old-key', 'user-1', '{\"payment_id\":\"payment-1\"}')"))
    run_revision(engine, MIGRATION)

    with pytest.raises(RuntimeError, match="cannot preserve migration metadata"):
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            MIGRATION.op = Operations(context)
            MIGRATION.downgrade()

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM idempotency_keys")).scalar_one() == 1


def test_empty_downgrade_restores_legacy_shape(tmp_path) -> None:
    engine = make_engine(tmp_path)
    run_revision(engine, OLD_REVISION)
    run_revision(engine, MIGRATION)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        MIGRATION.op = Operations(context)
        MIGRATION.downgrade()

    columns = {column["name"] for column in inspect(engine).get_columns("idempotency_keys")}
    assert columns == {"key", "user_id", "response_json", "created_at", "updated_at"}


def test_legacy_replay_requires_same_user_and_verified_transfer(tmp_path) -> None:
    engine = make_engine(tmp_path)
    Base.metadata.create_all(engine)
    with sa.orm.Session(engine) as session:
        owner = User(email="owner@example.com", display_name="Owner", password_hash="hash")
        debtor = User(email="debtor@example.com", display_name="Debtor", password_hash="hash")
        outsider = User(email="outsider@example.com", display_name="Outsider", password_hash="hash")
        session.add_all([owner, debtor, outsider])
        session.flush()
        group = Group(name="Group", created_by_id=owner.id)
        session.add(group)
        session.flush()
        session.add_all([
            GroupMember(group_id=group.id, user_id=owner.id, role="owner"),
            GroupMember(group_id=group.id, user_id=debtor.id, role="member"),
            GroupMember(group_id=group.id, user_id=outsider.id, role="member"),
        ])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR")
        session.add(settlement)
        session.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=100, currency="INR")
        other_transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=200, currency="INR")
        session.add_all([transfer, other_transfer])
        session.flush()
        payment = Payment(transfer_id=transfer.id, amount_minor=100, currency="INR", provider_order_id="ORDER-1", status="APPROVAL_PENDING")
        session.add(payment)
        session.flush()
        session.add(IdempotencyKey(
            key="legacy-key",
            user_id=debtor.id,
            operation="legacy",
            resource_id="",
            request_fingerprint="",
            response_json={"payment_id": payment.id, "order_id": "ORDER-1", "status": "APPROVAL_PENDING"},
        ))
        session.commit()

        replay = create_payment_order(transfer.id, object(), debtor, session, "legacy-key")
        assert replay["payment_id"] == payment.id
        with pytest.raises(HTTPException) as different_resource:
            create_payment_order(other_transfer.id, object(), debtor, session, "legacy-key")
        assert different_resource.value.status_code == 409
        with pytest.raises(HTTPException) as unauthorized:
            create_payment_order(transfer.id, object(), outsider, session, "legacy-key")
        assert unauthorized.value.status_code == 403
