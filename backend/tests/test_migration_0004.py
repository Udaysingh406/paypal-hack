"""Disposable PostgreSQL tests for durable payment-attempt storage."""

import os
from datetime import UTC, datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

MIGRATION = import_module("migrations.versions.0004_payment_attempts")
DATABASE_URL = os.getenv("SETTLEMATE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SETTLEMATE_TEST_POSTGRES_URL is not configured")


def make_engine():
    return sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")


def run_migration(db_engine, fn):
    with db_engine.begin() as connection:
        context = MigrationContext.configure(connection)
        MIGRATION.op = Operations(context)
        fn()


def assert_insert_fails(db_engine, statement):
    with pytest.raises(sa.exc.IntegrityError), db_engine.begin() as connection:
        connection.execute(sa.text(statement))


def create_schema(db_engine, *, populated=True):
    with db_engine.begin() as c:
        for table in ("payment_attempts", "idempotency_key_archive", "idempotency_keys", "payments", "transfers", "settlements", "users"):
            c.exec_driver_sql(f"DROP TABLE IF EXISTS {table} CASCADE")
        c.exec_driver_sql("CREATE TABLE users (id varchar(36) PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE settlements (id varchar(36) PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE transfers (id varchar(36) PRIMARY KEY, settlement_id varchar(36) NOT NULL REFERENCES settlements(id), amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, status varchar(32) NOT NULL)")
        c.exec_driver_sql("CREATE TABLE payments (id varchar(36) PRIMARY KEY, transfer_id varchar(36) NOT NULL UNIQUE REFERENCES transfers(id), amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, status varchar(32) NOT NULL)")
        c.exec_driver_sql("CREATE TABLE idempotency_keys (id varchar(36) PRIMARY KEY, key varchar(160) NOT NULL UNIQUE, user_id varchar(36) NOT NULL REFERENCES users(id), operation varchar(64) NOT NULL, resource_id varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL, response_json json NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL)")
        c.exec_driver_sql("CREATE TABLE idempotency_key_archive (original_id varchar(36) PRIMARY KEY, key varchar(160) NOT NULL, user_id varchar(36) NOT NULL, operation varchar(64) NOT NULL, resource_id varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL, response_json json NOT NULL, original_created_at timestamptz NOT NULL, original_updated_at timestamptz NOT NULL, archive_reason varchar(240) NOT NULL, archived_at timestamptz NOT NULL, financial_integrity_classification varchar(32) NOT NULL)")
        if populated:
            now = datetime(2025, 1, 1, tzinfo=UTC)
            c.execute(sa.text("INSERT INTO users VALUES ('user-1')"))
            c.execute(sa.text("INSERT INTO settlements VALUES ('settlement-1')"))
            c.execute(sa.text("INSERT INTO transfers VALUES ('transfer-1','settlement-1',501,'INR','pending')"))
            c.execute(sa.text("INSERT INTO payments VALUES ('payment-1','transfer-1',500,'INR','APPROVAL_PENDING')"))
            c.execute(sa.text("INSERT INTO idempotency_keys VALUES ('key-1','active-key','user-1','payment-order','transfer-1','fingerprint','{}',:now,:now)"), {"now": now})
            c.execute(sa.text("INSERT INTO idempotency_key_archive VALUES ('archive-1','archived-key','user-1','payment-order','transfer-1','fingerprint','{}',:now,:now,'duplicate active key; provider status unverified',:now,'amount_mismatch')"), {"now": now})


def test_upgrade_preserves_history_and_creates_schema():
    db_engine = make_engine(); create_schema(db_engine); run_migration(db_engine, MIGRATION.upgrade)
    with db_engine.connect() as c:
        assert c.execute(sa.text("SELECT amount_minor FROM payments")).scalar_one() == 500
        assert c.execute(sa.text("SELECT amount_minor FROM transfers")).scalar_one() == 501
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_keys")).scalar_one() == 1
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_key_archive")).scalar_one() == 1
        columns = {x["name"] for x in inspect(db_engine).get_columns("payment_attempts")}
        assert columns == {"id", "transfer_id", "user_id", "idempotency_key", "operation", "request_fingerprint", "amount_minor", "currency", "return_url", "cancel_url", "provider", "provider_request_id", "provider_order_id", "status", "error_classification", "created_at", "updated_at", "completed_at"}
        assert len(inspect(db_engine).get_foreign_keys("payment_attempts")) == 2
    db_engine.dispose()


def test_constraints_cover_provider_ids_active_attempts_and_nullable_keys():
    db_engine = make_engine(); create_schema(db_engine, populated=False); run_migration(db_engine, MIGRATION.upgrade)
    with db_engine.begin() as c:
        c.execute(sa.text("INSERT INTO users VALUES ('u')")); c.execute(sa.text("INSERT INTO settlements VALUES ('s')")); c.execute(sa.text("INSERT INTO transfers VALUES ('t','s',100,'INR','pending')"))
        values = {"id": "a1", "transfer": "t", "user": "u", "op": "payment-order", "fp": "fp", "request": "provider-request", "order": None, "status": "RESERVED"}
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,idempotency_key,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,provider_order_id,status) VALUES (:id,:transfer,:user,NULL,:op,:fp,100,'INR','paypal',:request,:order,:status)"), values)
    assert_insert_fails(db_engine, "INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a2','t','u','payment-order','fp',100,'INR','paypal','provider-request-2','RECONCILIATION_REQUIRED')")
    with db_engine.begin() as c:
        c.execute(sa.text("UPDATE payment_attempts SET status='COMPLETED' WHERE id='a1'"))
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a2','t','u','payment-order','fp',100,'INR','paypal','provider-request-2','RESERVED')"))
    assert_insert_fails(db_engine, "INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a3','t','u','payment-order','fp',100,'INR','paypal','provider-request-2','FAILED')")
    with db_engine.begin() as c:
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a4','t','u','other-operation','fp',100,'INR','paypal','provider-request-4','FAILED')"))
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a5','t','u','nullable-key','fp',100,'INR','paypal','provider-request-5','FAILED')"))
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a6','t','u','nullable-key-2','fp',100,'INR','paypal','provider-request-6','FAILED')"))
    db_engine.dispose()


def test_failed_upgrade_rolls_back_without_replacing_existing_table():
    db_engine = make_engine(); create_schema(db_engine, populated=False)
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE payment_attempts (marker integer)")
    with pytest.raises(RuntimeError, match="already exists"):
        run_migration(db_engine, MIGRATION.upgrade)
    with db_engine.connect() as c:
        assert inspect(db_engine).get_columns("payment_attempts")[0]["name"] == "marker"
    db_engine.dispose()


def test_populated_downgrade_refuses_before_drop():
    db_engine = make_engine(); create_schema(db_engine, populated=False); run_migration(db_engine, MIGRATION.upgrade)
    with db_engine.begin() as c:
        c.execute(sa.text("INSERT INTO users VALUES ('u')")); c.execute(sa.text("INSERT INTO settlements VALUES ('s')")); c.execute(sa.text("INSERT INTO transfers VALUES ('t','s',100,'INR','pending')"))
        c.execute(sa.text("INSERT INTO payment_attempts (id,transfer_id,user_id,operation,request_fingerprint,amount_minor,currency,provider,provider_request_id,status) VALUES ('a','t','u','payment-order','fp',100,'INR','paypal','request','RESERVED')"))
    with pytest.raises(RuntimeError, match="attempt history"):
        run_migration(db_engine, MIGRATION.downgrade)
    assert inspect(db_engine).has_table("payment_attempts")
    db_engine.dispose()
