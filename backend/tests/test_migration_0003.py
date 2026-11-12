"""Disposable PostgreSQL tests for the global idempotency recovery migration."""

import os
from datetime import UTC, datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

MIGRATION = import_module("migrations.versions.0003_global_idempotency_recovery")
DATABASE_URL = os.getenv("SETTLEMATE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="SETTLEMATE_TEST_POSTGRES_URL is not configured")


def run_migration(engine, fn):
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        MIGRATION.op = Operations(context)
        fn()


def fresh_engine():
    return sa.create_engine(DATABASE_URL, isolation_level="READ COMMITTED")


def create_schema(engine, *, composite=True, populated=True, nullable_timestamps=False):
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS idempotency_key_archive CASCADE")
        c.exec_driver_sql("DROP TABLE IF EXISTS idempotency_keys CASCADE")
        c.exec_driver_sql("DROP TABLE IF EXISTS payments CASCADE")
        c.exec_driver_sql("DROP TABLE IF EXISTS transfers CASCADE")
        c.exec_driver_sql("DROP TABLE IF EXISTS settlements CASCADE")
        c.exec_driver_sql("DROP TABLE IF EXISTS users CASCADE")
        c.exec_driver_sql("CREATE TABLE users (id varchar(36) PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE settlements (id varchar(36) PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE transfers (id varchar(36) PRIMARY KEY, settlement_id varchar(36) NOT NULL REFERENCES settlements(id), amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, status varchar(32) NOT NULL)")
        c.exec_driver_sql("CREATE TABLE payments (id varchar(36) PRIMARY KEY, transfer_id varchar(36) NOT NULL UNIQUE REFERENCES transfers(id), amount_minor bigint NOT NULL, currency varchar(3) NOT NULL, status varchar(32) NOT NULL)")
        unique = "UNIQUE (user_id, key, operation, resource_id)" if composite else "UNIQUE (key)"
        timestamp_nullability = "" if nullable_timestamps else " NOT"
        c.exec_driver_sql(f"""CREATE TABLE idempotency_keys (
            id varchar(36) PRIMARY KEY, key varchar(160) NOT NULL,
            user_id varchar(36) NOT NULL REFERENCES users(id), operation varchar(64) NOT NULL,
            resource_id varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL,
            response_json json NOT NULL, created_at timestamptz{timestamp_nullability} NULL,
            updated_at timestamptz{timestamp_nullability} NULL, {unique})""")
        if populated:
            now = datetime(2025, 1, 1, tzinfo=UTC)
            c.execute(sa.text("INSERT INTO users VALUES (:id)"), [{"id": f"u-{i}"} for i in range(5)])
            c.execute(sa.text("INSERT INTO settlements VALUES (:id)"), [{"id": f"s-{i}"} for i in range(17)])
            c.execute(sa.text("INSERT INTO transfers VALUES (:id, :settlement, :amount, 'INR', 'pending')"), [
                {"id": f"t-{i}", "settlement": f"s-{i}", "amount": 501 if i in (0, 1, 3, 4) else 500}
                for i in range(17)
            ])
            c.execute(sa.text("INSERT INTO payments VALUES (:id, :transfer, :amount, 'INR', 'APPROVAL_PENDING')"), [
                {"id": f"p-{i}", "transfer": f"t-{i}", "amount": 500 if i < 14 else 501}
                for i in range(17)
            ])
            rows = []
            for i in range(17):
                rows.append({"id": f"ik-{i}", "key": "duplicate" if i < 5 else f"key-{i}", "user": f"u-{i % 5}", "resource": f"t-{i}", "response": f'{{"payment_id":"p-{i}"}}', "created": now, "updated": now})
            c.execute(sa.text("INSERT INTO idempotency_keys VALUES (:id,:key,:user,'payment_order',:resource,'fp',CAST(:response AS json),:created,:updated)"), rows)


def test_upgrade_archives_five_and_preserves_other_data():
    engine = fresh_engine(); create_schema(engine); run_migration(engine, MIGRATION.upgrade)
    with engine.connect() as c:
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_key_archive")).scalar_one() == 5
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_keys")).scalar_one() == 12
        assert c.execute(sa.text("SELECT COUNT(*) FROM payments WHERE amount_minor <> 501 AND id LIKE 'p-%'")).scalar_one() == 14
        assert c.execute(sa.text("SELECT COUNT(*) FROM transfers WHERE amount_minor = 501")).scalar_one() == 4
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_key_archive WHERE financial_integrity_classification='amount_mismatch'")).scalar_one() == 4
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_key_archive WHERE financial_integrity_classification='consistent'")).scalar_one() == 1
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_key_archive WHERE archive_reason LIKE '%provider status unverified'")).scalar_one() == 5
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_keys WHERE key='duplicate'")).scalar_one() == 0
        assert len(inspect(engine).get_pk_constraint("idempotency_keys")["constrained_columns"]) == 1
        assert any(set(x["column_names"]) == {"key"} for x in inspect(engine).get_unique_constraints("idempotency_keys"))
        assert not any(set(x["column_names"]) == {"user_id", "key", "operation", "resource_id"} for x in inspect(engine).get_unique_constraints("idempotency_keys"))
    engine.dispose()


def test_null_timestamp_rolls_back_without_archive():
    engine = fresh_engine(); create_schema(engine, populated=False, nullable_timestamps=True)
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO users VALUES ('u-1')")); c.execute(sa.text("INSERT INTO idempotency_keys VALUES ('i-1','k','u-1','op','r','fp','{}',NULL,NULL)"))
    with pytest.raises(RuntimeError, match="null timestamps"):
        run_migration(engine, MIGRATION.upgrade)
    with engine.connect() as c:
        assert not inspect(engine).has_table("idempotency_key_archive")
        assert c.execute(sa.text("SELECT COUNT(*) FROM idempotency_keys")).scalar_one() == 1
    engine.dispose()


def test_populated_downgrade_refuses_before_destructive_ddl():
    engine = fresh_engine(); create_schema(engine); run_migration(engine, MIGRATION.upgrade)
    with pytest.raises(RuntimeError, match="financial history"):
        run_migration(engine, MIGRATION.downgrade)
    assert inspect(engine).has_table("idempotency_key_archive")
    engine.dispose()


def test_clean_global_schema_path():
    engine = fresh_engine(); create_schema(engine, composite=False, populated=False); run_migration(engine, MIGRATION.upgrade)
    assert inspect(engine).has_table("idempotency_key_archive")
    engine.dispose()
