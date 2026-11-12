"""Disposable PostgreSQL tests for capture-attempt schema migration."""

import os
from datetime import UTC, datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

MIGRATION = import_module("migrations.versions.0005_capture_attempts")
DATABASE_URL = os.getenv("SETTLEMATE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SETTLEMATE_TEST_POSTGRES_URL is not configured")


def engine():
    return sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")


def migrate(db_engine, operation):
    with db_engine.begin() as connection:
        MIGRATION.op = Operations(MigrationContext.configure(connection))
        operation()


def base_schema(db_engine):
    with db_engine.begin() as c:
        for table in ("capture_attempts", "payment_attempts", "payments", "transfers", "settlements", "users"):
            c.exec_driver_sql(f"DROP TABLE IF EXISTS {table} CASCADE")
        c.exec_driver_sql("CREATE TABLE users (id varchar(36) PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE settlements (id varchar(36) PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE transfers (id varchar(36) PRIMARY KEY, settlement_id varchar(36) NOT NULL REFERENCES settlements(id), amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, status varchar(32) NOT NULL)")
        c.exec_driver_sql("CREATE TABLE payments (id varchar(36) PRIMARY KEY, transfer_id varchar(36) NOT NULL UNIQUE REFERENCES transfers(id), amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, status varchar(32) NOT NULL)")
        c.exec_driver_sql("CREATE TABLE payment_attempts (id varchar(36) PRIMARY KEY, transfer_id varchar(36) NOT NULL REFERENCES transfers(id), user_id varchar(36) NOT NULL REFERENCES users(id), idempotency_key varchar(160), operation varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL, amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, return_url varchar(2048), cancel_url varchar(2048), provider varchar(32) NOT NULL, provider_request_id varchar(108) NOT NULL UNIQUE, provider_order_id varchar(128) UNIQUE, status varchar(32) NOT NULL, error_classification varchar(64), created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, completed_at timestamptz)")
        now = datetime(2025, 1, 1, tzinfo=UTC)
        c.execute(sa.text("INSERT INTO users VALUES ('u')"))
        c.execute(sa.text("INSERT INTO settlements VALUES ('s')"))
        c.execute(sa.text("INSERT INTO transfers VALUES ('t','s',100,'INR','pending')"))
        c.execute(sa.text("INSERT INTO payments VALUES ('p','t',100,'INR','APPROVAL_PENDING')"))
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,provider_order_id,status,created_at,updated_at) VALUES ('a','t','u','payment-order','fp',100,'INR','paypal','order-request','order-1','COMPLETED',:now,:now)"), {"now": now})


def test_upgrade_preserves_history_and_creates_expected_schema():
    db = engine(); base_schema(db); migrate(db, MIGRATION.upgrade)
    with db.connect() as c:
        assert c.execute(sa.text("SELECT amount_minor FROM payments")).scalar_one() == 100
        assert c.execute(sa.text("SELECT provider_order_id FROM payment_attempts")).scalar_one() == "order-1"
        assert {x["name"] for x in inspect(db).get_columns("capture_attempts")} == {"id", "payment_id", "transfer_id", "parent_attempt_id", "provider", "provider_order_id", "provider_request_id", "provider_capture_id", "amount_minor", "currency", "status", "reconciliation_state", "provider_capture_status", "error_classification", "created_at", "updated_at", "completed_at"}
        assert len(inspect(db).get_foreign_keys("capture_attempts")) == 3
    db.dispose()


def test_constraints_enforce_parent_context_active_and_completed_uniqueness():
    db = engine(); base_schema(db); migrate(db, MIGRATION.upgrade)
    with db.begin() as c:
        c.execute(sa.text("INSERT INTO capture_attempts (id,payment_id,transfer_id,parent_attempt_id,provider,provider_order_id,provider_request_id,amount_minor,currency,status,reconciliation_state) VALUES ('c1','p','t','a','paypal','order-1','capture-request',100,'INR','RESERVED','NOT_REQUIRED')"))
    with pytest.raises(sa.exc.IntegrityError), db.begin() as c:
        c.execute(sa.text("INSERT INTO capture_attempts (id,payment_id,transfer_id,parent_attempt_id,provider,provider_order_id,provider_request_id,amount_minor,currency,status,reconciliation_state) VALUES ('c2','p','t','a','paypal','order-1','capture-request-2',100,'INR','PROVIDER_REQUESTED','NOT_REQUIRED')"))
    with db.begin() as c:
        c.execute(sa.text("UPDATE capture_attempts SET status='COMPLETED', provider_capture_id='capture-1', completed_at=now() WHERE id='c1'"))
    with pytest.raises(sa.exc.IntegrityError), db.begin() as c:
        c.execute(sa.text("INSERT INTO capture_attempts (id,payment_id,transfer_id,parent_attempt_id,provider,provider_order_id,provider_request_id,amount_minor,currency,status,reconciliation_state,provider_capture_id,completed_at) VALUES ('c3','p','t','a','paypal','order-1','capture-request-3',100,'INR','COMPLETED','NOT_REQUIRED','capture-2',now())"))
    db.dispose()


def test_provider_ids_and_parent_fk_are_protected():
    db = engine(); base_schema(db); migrate(db, MIGRATION.upgrade)
    with pytest.raises(sa.exc.IntegrityError), db.begin() as c:
        c.execute(sa.text("INSERT INTO capture_attempts (id,payment_id,transfer_id,parent_attempt_id,provider,provider_order_id,provider_request_id,amount_minor,currency,status,reconciliation_state) VALUES ('bad','p','t','missing','paypal','order-1','unique',100,'INR','RESERVED','NOT_REQUIRED')"))
    db.dispose()


def test_populated_downgrade_refuses_before_destructive_ddl():
    db = engine(); base_schema(db); migrate(db, MIGRATION.upgrade)
    with db.begin() as c:
        c.execute(sa.text("INSERT INTO capture_attempts (id,payment_id,transfer_id,parent_attempt_id,provider,provider_order_id,provider_request_id,amount_minor,currency,status,reconciliation_state) VALUES ('c','p','t','a','paypal','order-1','capture-request',100,'INR','RESERVED','NOT_REQUIRED')"))
    with pytest.raises(RuntimeError, match="capture history"):
        migrate(db, MIGRATION.downgrade)
    assert inspect(db).has_table("capture_attempts")
    db.dispose()


def test_empty_downgrade_removes_capture_schema_and_parent_constraint():
    db = engine(); base_schema(db); migrate(db, MIGRATION.upgrade); migrate(db, MIGRATION.downgrade)
    assert not inspect(db).has_table("capture_attempts")
    assert "uq_payment_attempt_context" not in {x["name"] for x in inspect(db).get_unique_constraints("payment_attempts")}
    db.dispose()
