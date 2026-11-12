from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.db import SessionLocal, engine
from app.main import app
from app.models import Transfer


def register(client: TestClient, label: str) -> tuple[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": f"{label}-{uuid4().hex}@example.com", "display_name": label, "password": "password123"},
    )
    assert response.status_code == 201
    data = response.json()
    return data["access_token"], data["user_id"]


def test_production_rejects_default_or_short_auth_secret() -> None:
    with pytest.raises(ValueError, match="AUTH_SECRET"):
        Settings(app_env="production", auth_secret="change-me-in-development")
    with pytest.raises(ValueError, match="AUTH_SECRET"):
        Settings(app_env="production", auth_secret="too-short")


def test_production_accepts_a_sufficiently_long_auth_secret() -> None:
    settings = Settings(app_env="production", auth_secret="production-secret-" + "x" * 40)
    assert len(settings.auth_secret.get_secret_value()) >= 32


def test_only_owner_or_admin_can_approve_settlement() -> None:
    with TestClient(app) as client:
        owner_token, owner_id = register(client, "owner")
        member_token, member_id = register(client, "member")
        headers = {"Authorization": f"Bearer {owner_token}"}
        group = client.post("/api/v1/groups", headers=headers, json={"name": f"security-{uuid4().hex}"}).json()
        group_id = group["id"]
        added = client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": member_id, "role": "member"},
        )
        assert added.status_code == 201
        invalid_role = client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": member_id, "role": "owner"},
        )
        assert invalid_role.status_code == 422
        invalid_admin_role = client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": member_id, "role": "admin"},
        )
        assert invalid_admin_role.status_code == 422

        expense = client.post(
            f"/api/v1/groups/{group_id}/expenses",
            headers=headers,
            json={
                "description": "Dinner",
                "amount_minor": 1000,
                "currency": "INR",
                "paid_by_id": owner_id,
                "splits": {owner_id: 500, member_id: 500},
            },
        )
        assert expense.status_code == 201
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers)
        assert settlement.status_code == 201
        settlement_id = settlement.json()["id"]

        denied = client.post(
            f"/api/v1/settlements/{settlement_id}/approve",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert denied.status_code == 403
        approved = client.post(f"/api/v1/settlements/{settlement_id}/approve", headers=headers)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"


def test_payment_order_rejects_cross_user_and_resource_key_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE IF NOT EXISTS idempotency_key_archive (
            original_id varchar(36) PRIMARY KEY, key varchar(160) NOT NULL,
            user_id varchar(36) NOT NULL, operation varchar(64) NOT NULL,
            resource_id varchar(64) NOT NULL, request_fingerprint varchar(64) NOT NULL,
            response_json json NOT NULL, original_created_at datetime NOT NULL,
            original_updated_at datetime NOT NULL, archive_reason varchar(240) NOT NULL,
            archived_at datetime NOT NULL, financial_integrity_classification varchar(32) NOT NULL)"""))
    def fake_create_order(self, amount_minor: int, currency: str, return_url: str, cancel_url: str, request_id: str | None = None) -> dict:
        return {"id": f"ORDER-{uuid4().hex}", "purchase_units": [{"amount": {"currency_code": currency, "value": f"{amount_minor / 100:.2f}"}}], "links": [{"rel": "approve", "href": "https://paypal.test/approve"}]}

    monkeypatch.setattr("app.main.PayPalClient.create_order", fake_create_order)
    with TestClient(app) as client:
        owner_token, owner_id = register(client, "owner")
        debtor_token, debtor_id = register(client, "debtor")
        other_debtor_token, other_debtor_id = register(client, "other-debtor")
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        debtor_headers = {"Authorization": f"Bearer {debtor_token}"}
        other_debtor_headers = {"Authorization": f"Bearer {other_debtor_token}"}
        idempotency_key = f"shared-key-{uuid4().hex}"
        group_id = client.post(
            "/api/v1/groups",
            headers=owner_headers,
            json={"name": f"payments-{uuid4().hex}"},
        ).json()["id"]
        assert client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=owner_headers,
            json={"user_id": debtor_id, "role": "member"},
        ).status_code == 201
        assert client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=owner_headers,
            json={"user_id": other_debtor_id, "role": "member"},
        ).status_code == 201
        assert client.post(
            f"/api/v1/groups/{group_id}/expenses",
            headers=owner_headers,
            json={
                "description": "Dinner",
                "amount_minor": 1500,
                "currency": "INR",
                "paid_by_id": owner_id,
                "splits": {owner_id: 500, debtor_id: 500, other_debtor_id: 500},
            },
        ).status_code == 201
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=owner_headers).json()
        listed = client.get(f"/api/v1/groups/{group_id}/settlements", headers=owner_headers).json()
        settlement_id = settlement["id"]
        assert client.post(f"/api/v1/settlements/{settlement_id}/approve", headers=owner_headers).status_code == 200
        transfers = {item["from_user_id"]: item["id"] for item in listed[0]["transfers"]}
        transfer_id = transfers[debtor_id]
        other_transfer_id = transfers[other_debtor_id]

        forbidden = client.post(
            f"/api/v1/transfers/{transfer_id}/payment-order",
            headers={**owner_headers, "Idempotency-Key": idempotency_key},
        )
        assert forbidden.status_code == 403
        first = client.post(
            f"/api/v1/transfers/{transfer_id}/payment-order",
            headers={**debtor_headers, "Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 200
        replay = client.post(
            f"/api/v1/transfers/{transfer_id}/payment-order",
            headers={**debtor_headers, "Idempotency-Key": idempotency_key},
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        cross_user = client.post(
            f"/api/v1/transfers/{other_transfer_id}/payment-order",
            headers={**other_debtor_headers, "Idempotency-Key": idempotency_key},
        )
        assert cross_user.status_code == 409
        db = SessionLocal()
        try:
            second_transfer = Transfer(
                settlement_id=settlement_id,
                from_user_id=debtor_id,
                to_user_id=owner_id,
                amount_minor=500,
                currency="INR",
            )
            db.add(second_transfer)
            db.commit()
            second_transfer_id = second_transfer.id
        finally:
            db.close()
        different_transfer = client.post(
            f"/api/v1/transfers/{second_transfer_id}/payment-order",
            headers={**debtor_headers, "Idempotency-Key": idempotency_key},
        )
        assert different_transfer.status_code == 409
        db = SessionLocal()
        try:
            transfer = db.get(Transfer, transfer_id)
            transfer.amount_minor += 1
            db.commit()
        finally:
            db.close()
        conflict = client.post(
            f"/api/v1/transfers/{transfer_id}/payment-order",
            headers={**debtor_headers, "Idempotency-Key": idempotency_key},
        )
        assert conflict.status_code == 409
