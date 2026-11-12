from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Payment, PaymentAttempt, Transfer
from app.services.idempotency import validate_new_payment_integrity


def register(client: TestClient, label: str) -> tuple[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{label}-{uuid4().hex}@example.com",
            "display_name": label,
            "password": "password123",
        },
    )
    assert response.status_code == 201
    data = response.json()
    return data["access_token"], data["user_id"]


def make_group(client: TestClient) -> tuple[dict[str, str], str, str, str]:
    owner_token, owner_id = register(client, "owner")
    _, member_id = register(client, "member")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    group = client.post(
        "/api/v1/groups",
        headers=owner_headers,
        json={"name": f"financial-{uuid4().hex}"},
    ).json()
    assert client.post(
        f"/api/v1/groups/{group['id']}/members",
        headers=owner_headers,
        json={"user_id": member_id, "role": "member"},
    ).status_code == 201
    return owner_headers, group["id"], owner_id, member_id


def add_expense(client: TestClient, group_id: str, headers: dict[str, str], payer: str, other: str, currency: str, amount: int = 1000):
    return client.post(
        f"/api/v1/groups/{group_id}/expenses",
        headers=headers,
        json={
            "description": "Dinner",
            "amount_minor": amount,
            "currency": currency,
            "paid_by_id": payer,
            "splits": {payer: amount // 2, other: amount - amount // 2},
        },
    )


def test_mixed_currency_is_rejected_and_same_currency_is_reported() -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        assert add_expense(client, group_id, headers, owner_id, member_id, "inr").status_code == 201
        assert add_expense(client, group_id, headers, owner_id, member_id, "USD").status_code == 201
        balances = client.get(f"/api/v1/groups/{group_id}/balances", headers=headers)
        assert balances.status_code == 409
        assert client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).status_code == 409

    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        assert add_expense(client, group_id, headers, owner_id, member_id, "inr").status_code == 201
        balances = client.get(f"/api/v1/groups/{group_id}/balances", headers=headers)
        assert balances.status_code == 200
        assert balances.json()["currency"] == "INR"


def test_currency_allowlist_and_expense_delete_authorization() -> None:
    with TestClient(app) as client:
        owner_headers, group_id, owner_id, member_id = make_group(client)
        unsupported = add_expense(client, group_id, owner_headers, owner_id, member_id, "JPY")
        assert unsupported.status_code == 422
        expense = add_expense(client, group_id, owner_headers, owner_id, member_id, "USD")
        member_token, delete_member_id = register(client, "delete-member")
        assert client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=owner_headers,
            json={"user_id": delete_member_id, "role": "member"},
        ).status_code == 201
        assert client.delete(
            f"/api/v1/expenses/{expense.json()['id']}",
            headers={"Authorization": f"Bearer {member_token}"},
        ).status_code == 403


def test_recalculation_supersedes_obsolete_unpaid_transfers() -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        assert add_expense(client, group_id, headers, owner_id, member_id, "INR", 1000).status_code == 201
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        listed = client.get(f"/api/v1/groups/{group_id}/settlements", headers=headers).json()
        old_transfer_id = listed[0]["transfers"][0]["id"]
        assert add_expense(client, group_id, headers, member_id, owner_id, "INR", 200).status_code == 201
        assert client.post(
            f"/api/v1/settlements/{settlement['id']}/recalculate",
            headers=headers,
        ).status_code == 200
        db = SessionLocal()
        try:
            old_transfer = db.get(Transfer, old_transfer_id)
            assert old_transfer is not None
            assert old_transfer.status == "superseded"
        finally:
            db.close()


def test_recalculation_is_idempotent_and_approved_records_are_immutable() -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        expense = add_expense(client, group_id, headers, owner_id, member_id, "INR")
        expense_id = expense.json()["id"]
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        settlement_id = settlement["id"]
        first = client.post(f"/api/v1/settlements/{settlement_id}/recalculate", headers=headers)
        second = client.post(f"/api/v1/settlements/{settlement_id}/recalculate", headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["transfer_count"] == second.json()["transfer_count"] == 1
        db = SessionLocal()
        try:
            transfers = db.query(Transfer).filter(Transfer.settlement_id == settlement_id, Transfer.status == "pending").all()
            assert len(transfers) == 1
        finally:
            db.close()

        assert client.post(f"/api/v1/settlements/{settlement_id}/approve", headers=headers).status_code == 200
        assert client.post(f"/api/v1/settlements/{settlement_id}/recalculate", headers=headers).status_code == 409
        assert client.delete(f"/api/v1/expenses/{expense_id}", headers=headers).status_code == 409


def test_completed_payment_backed_transfer_survives_recalculation_and_lifecycle_is_explicit() -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        expense = add_expense(client, group_id, headers, owner_id, member_id, "INR")
        assert expense.json()["status"] == "active"
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        transfer_id = settlement["transfers"][0]["id"] if settlement["transfers"] and "id" in settlement["transfers"][0] else None
        listed = client.get(f"/api/v1/groups/{group_id}/settlements", headers=headers).json()
        transfer_id = listed[0]["transfers"][0]["id"]
        db = SessionLocal()
        try:
            transfer = db.get(Transfer, transfer_id)
            transfer.status = "completed"
            db.add(Payment(transfer_id=transfer.id, amount_minor=transfer.amount_minor, currency=transfer.currency, status="COMPLETED"))
            db.commit()
        finally:
            db.close()
        recalculated = client.post(f"/api/v1/settlements/{settlement['id']}/recalculate", headers=headers)
        assert recalculated.status_code == 200
        db = SessionLocal()
        try:
            assert db.get(Transfer, transfer_id) is not None
            assert db.query(Transfer).filter(
                Transfer.settlement_id == settlement["id"], Transfer.status == "pending"
            ).count() == 0
        finally:
            db.close()

        outsider_token, _ = register(client, "outsider")
        denied = client.delete(
            f"/api/v1/expenses/{expense.json()['id']}",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        assert denied.status_code == 404


def test_recalculation_blocks_provider_backed_pending_payment() -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        add_expense(client, group_id, headers, owner_id, member_id, "INR")
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        listed = client.get(f"/api/v1/groups/{group_id}/settlements", headers=headers).json()
        transfer_id = listed[0]["transfers"][0]["id"]
        with SessionLocal() as db:
            transfer = db.get(Transfer, transfer_id)
            order_id = f"provider-order-{uuid4().hex}"
            payment = Payment(transfer_id=transfer.id, provider_order_id=order_id, amount_minor=transfer.amount_minor, currency=transfer.currency, status="APPROVAL_PENDING")
            db.add(payment); db.flush()
            db.add(PaymentAttempt(transfer_id=transfer.id, user_id=transfer.from_user_id, operation="payment-order", request_fingerprint="fp", amount_minor=transfer.amount_minor, currency=transfer.currency, provider="paypal", provider_request_id=f"provider-request-{uuid4().hex}", provider_order_id=order_id, status="PROVIDER_REQUESTED"))
            db.commit()
        response = client.post(f"/api/v1/settlements/{settlement['id']}/recalculate", headers=headers)
        assert response.status_code == 409
        with SessionLocal() as db:
            assert db.get(Transfer, transfer_id).status == "pending"


@pytest.mark.parametrize(
    "payment_amount,payment_currency,payment_status",
    [(99, "INR", "APPROVAL_PENDING"), (100, "USD", "APPROVAL_PENDING"), (100, "INR", "COMPLETED")],
)
def test_new_payment_integrity_rejects_mismatch_or_invalid_lifecycle(payment_amount, payment_currency, payment_status) -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        add_expense(client, group_id, headers, owner_id, member_id, "INR")
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        transfer_id = client.get(f"/api/v1/groups/{group_id}/settlements", headers=headers).json()[0]["transfers"][0]["id"]
        with SessionLocal() as db:
            transfer = db.get(Transfer, transfer_id)
            candidate = Payment(transfer_id=transfer.id, amount_minor=payment_amount, currency=payment_currency, status=payment_status)
            with pytest.raises(Exception, match="Payment recovery required"):
                validate_new_payment_integrity(candidate, transfer)
            assert db.get(Transfer, transfer_id).amount_minor == transfer.amount_minor
        assert settlement["status"] == "proposed"


def test_historical_payment_mismatch_remains_readable_and_unchanged() -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        add_expense(client, group_id, headers, owner_id, member_id, "INR", 1002)
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        transfer_id = client.get(f"/api/v1/groups/{group_id}/settlements", headers=headers).json()[0]["transfers"][0]["id"]
        with SessionLocal() as db:
            transfer = db.get(Transfer, transfer_id)
            historical = Payment(transfer_id=transfer.id, amount_minor=500, currency="INR", status="APPROVAL_PENDING")
            db.add(historical); db.commit(); payment_id = historical.id
        response = client.get(f"/api/v1/payments/{payment_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["amount_minor"] == 500
        with SessionLocal() as db:
            assert db.get(Payment, payment_id).amount_minor == 500
            assert db.get(Transfer, transfer_id).amount_minor == 501
        assert settlement["status"] == "proposed"


@pytest.mark.parametrize("attempt_status", ["PROVIDER_REQUESTED", "PERSISTENCE_PENDING", "RECONCILIATION_REQUIRED"])
def test_recalculation_blocks_each_provider_order_attempt_state(attempt_status: str) -> None:
    with TestClient(app) as client:
        headers, group_id, owner_id, member_id = make_group(client)
        add_expense(client, group_id, headers, owner_id, member_id, "INR")
        settlement = client.post(f"/api/v1/groups/{group_id}/settlements/optimize", headers=headers).json()
        transfer_id = client.get(f"/api/v1/groups/{group_id}/settlements", headers=headers).json()[0]["transfers"][0]["id"]
        with SessionLocal() as db:
            transfer = db.get(Transfer, transfer_id)
            payment = Payment(transfer_id=transfer.id, amount_minor=transfer.amount_minor, currency=transfer.currency, status="APPROVAL_PENDING")
            db.add(payment); db.flush()
            parent = PaymentAttempt(transfer_id=transfer.id, user_id=transfer.from_user_id, operation="payment-order", request_fingerprint="fp", amount_minor=transfer.amount_minor, currency=transfer.currency, provider="paypal", provider_request_id=f"request-{uuid4().hex}", provider_order_id=None, status=attempt_status)
            db.add(parent); db.commit()
        assert client.post(f"/api/v1/settlements/{settlement['id']}/recalculate", headers=headers).status_code == 409
