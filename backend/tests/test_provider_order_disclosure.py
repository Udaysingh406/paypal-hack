"""Payment response tests for provider-order identifier disclosure."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import Base
from app.main import get_payment
from app.models import Group, GroupMember, Payment, Settlement, Transfer, User


def fixture_db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'provider-order-disclosure.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        debtor = User(email="debtor@example.com", display_name="Debtor", password_hash="hash")
        owner = User(email="owner@example.com", display_name="Owner", password_hash="hash")
        admin = User(email="admin@example.com", display_name="Admin", password_hash="hash")
        member = User(email="member@example.com", display_name="Member", password_hash="hash")
        outsider = User(email="outsider@example.com", display_name="Outsider", password_hash="hash")
        db.add_all([debtor, owner, admin, member, outsider]); db.flush()
        group = Group(name="Group", created_by_id=owner.id); db.add(group); db.flush()
        db.add_all([
            GroupMember(group_id=group.id, user_id=debtor.id),
            GroupMember(group_id=group.id, user_id=owner.id, role="owner"),
            GroupMember(group_id=group.id, user_id=admin.id, role="admin"),
            GroupMember(group_id=group.id, user_id=member.id, role="member"),
        ])
        settlement = Settlement(group_id=group.id, status="approved", currency="INR"); db.add(settlement); db.flush()
        transfer = Transfer(settlement_id=settlement.id, from_user_id=debtor.id, to_user_id=owner.id, amount_minor=100, currency="INR", status="pending"); db.add(transfer); db.flush()
        payment = Payment(transfer_id=transfer.id, amount_minor=100, currency="INR", provider_order_id="provider-order-secret", status="APPROVAL_PENDING"); db.add(payment); db.commit()
        return engine, SimpleNamespace(payment=payment.id, debtor=debtor.id, owner=owner.id, admin=admin.id, member=member.id, outsider=outsider.id)


def test_debtor_receives_provider_order_id(tmp_path):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        response = get_payment(ids.payment, db.get(User, ids.debtor), db)
    assert response["order_id"] == "provider-order-secret"
    engine.dispose()


def test_owner_and_admin_receive_provider_order_id(tmp_path):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        owner_response = get_payment(ids.payment, db.get(User, ids.owner), db)
        admin_response = get_payment(ids.payment, db.get(User, ids.admin), db)
    assert owner_response["order_id"] == "provider-order-secret"
    assert admin_response["order_id"] == "provider-order-secret"
    engine.dispose()


def test_regular_group_member_gets_safe_payment_status_without_provider_id(tmp_path):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db:
        response = get_payment(ids.payment, db.get(User, ids.member), db)
    assert "order_id" not in response
    assert response["status"] == "APPROVAL_PENDING"
    assert response["amount_minor"] == 100
    engine.dispose()


def test_unrelated_user_cannot_retrieve_payment(tmp_path):
    engine, ids = fixture_db(tmp_path)
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        get_payment(ids.payment, db.get(User, ids.outsider), db)
    assert error.value.status_code == 404
    engine.dispose()
