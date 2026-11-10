from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import create_token, get_current_user, hash_password, verify_password
from app.core.config import get_settings
from app.db import Base, engine, get_db
from app.models import (
    Expense,
    ExpenseSplit,
    Group,
    GroupMember,
    Payment,
    Settlement,
    Transfer,
    User,
    WebhookEvent,
)
from app.schemas import ChatRequest, ExpenseCreate, GroupCreate, LoginRequest, MemberCreate, UserCreate
from app.services.agent import AgentOrchestrator
from app.services.groups import audit, require_admin, require_member
from app.services.payments import PayPalClient
from app.services.settlement import calculate_balances, optimize_settlement

settings = get_settings()
app = FastAPI(title="SettleMate API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
orchestrator = AgentOrchestrator()


@app.on_event("startup")
def startup() -> None:
    if settings.app_env == "development" and settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(engine)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


def _check_database(db: Session) -> None:
    try:
        db.execute(select(1))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    _check_database(db)
    return {"status": "ok", "service": "settlemate-api", "database": "ok"}


@app.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    _check_database(db)
    return {"status": "ok", "service": "settlemate-api", "database": "ok"}


@app.get("/api/v1/health")
def api_health() -> dict[str, str]:
    return {"status": "ok", "service": "settlemate-api"}


@app.post("/api/v1/auth/register", status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> dict[str, str]:
    if db.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(409, "Email already registered")
    user = User(email=payload.email.lower(), display_name=payload.display_name, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": create_token(user.id), "token_type": "bearer", "user_id": user.id}


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": create_token(user.id), "token_type": "bearer", "user_id": user.id}


@app.post("/api/v1/groups", status_code=201)
def create_group(payload: GroupCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    group = Group(name=payload.name, created_by_id=user.id)
    db.add(group)
    db.flush()
    db.add(GroupMember(group_id=group.id, user_id=user.id, role="owner"))
    audit(db, user.id, "group.created", "group", group.id)
    db.commit()
    return {"id": group.id, "name": group.name, "created_by_id": user.id}


@app.get("/api/v1/groups")
def list_groups(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    groups = db.scalars(select(Group).join(GroupMember).where(GroupMember.user_id == user.id)).all()
    return [{"id": group.id, "name": group.name} for group in groups]


@app.get("/api/v1/groups/{group_id}")
def get_group(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    group = require_member(db, group_id, user.id)
    members = db.scalars(select(User).join(GroupMember).where(GroupMember.group_id == group_id)).all()
    return {"id": group.id, "name": group.name, "members": [{"id": item.id, "display_name": item.display_name} for item in members]}


@app.post("/api/v1/groups/{group_id}/members", status_code=201)
def add_member(group_id: str, payload: MemberCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    group = require_admin(db, group_id, user.id)
    target = db.get(User, payload.user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id == target.id)):
        raise HTTPException(409, "User is already a member")
    member = GroupMember(group_id=group.id, user_id=target.id, role=payload.role)
    db.add(member)
    audit(db, user.id, "group.member_added", "group", group.id, {"user_id": target.id})
    db.commit()
    return {"user_id": target.id, "role": member.role}


@app.post("/api/v1/groups/{group_id}/expenses", status_code=201)
def create_expense(group_id: str, payload: ExpenseCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    require_member(db, group_id, user.id)
    member_ids = set(db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id)).all())
    if payload.paid_by_id not in member_ids or set(payload.splits) != member_ids:
        raise HTTPException(400, "Payer and every split participant must belong to the group")
    if any(value <= 0 for value in payload.splits.values()) or sum(payload.splits.values()) != payload.amount_minor:
        raise HTTPException(400, "Splits must be positive and sum to the expense amount")
    expense = Expense(group_id=group_id, paid_by_id=payload.paid_by_id, description=payload.description, amount_minor=payload.amount_minor, currency=payload.currency)
    db.add(expense)
    db.flush()
    for participant, amount in payload.splits.items():
        db.add(ExpenseSplit(expense_id=expense.id, user_id=participant, amount_minor=amount))
    audit(db, user.id, "expense.created", "expense", expense.id)
    db.commit()
    return {"id": expense.id, "amount_minor": expense.amount_minor, "currency": expense.currency}


@app.get("/api/v1/groups/{group_id}/expenses")
def list_expenses(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    require_member(db, group_id, user.id)
    expenses = db.scalars(select(Expense).where(Expense.group_id == group_id).order_by(Expense.created_at.desc())).all()
    return [{"id": item.id, "description": item.description, "amount_minor": item.amount_minor, "currency": item.currency, "paid_by_id": item.paid_by_id} for item in expenses]


@app.delete("/api/v1/expenses/{expense_id}", status_code=204)
def delete_expense(expense_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    expense = db.get(Expense, expense_id)
    if not expense:
        raise HTTPException(404, "Expense not found")
    require_member(db, expense.group_id, user.id)
    if expense.status != "draft":
        raise HTTPException(409, "Only draft expenses can be deleted")
    expense.status = "deleted"
    audit(db, user.id, "expense.deleted", "expense", expense.id)
    db.commit()


@app.get("/api/v1/groups/{group_id}/balances")
def balances(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    require_member(db, group_id, user.id)
    members = db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id)).all()
    contributions = {member: 0 for member in members}
    shares = {member: 0 for member in members}
    for expense in db.scalars(select(Expense).where(Expense.group_id == group_id, Expense.status == "active")).all():
        contributions[expense.paid_by_id] += expense.amount_minor
        for split in expense.splits:
            shares[split.user_id] += split.amount_minor
    return {"balances": calculate_balances(contributions, shares)}


@app.post("/api/v1/groups/{group_id}/settlements/optimize", status_code=201)
def optimize(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    require_member(db, group_id, user.id)
    balance_data = balances(group_id, user, db)["balances"]
    group_expense = db.scalar(select(Expense).where(Expense.group_id == group_id))
    currency = group_expense.currency if group_expense else "INR"
    settlement = Settlement(group_id=group_id, currency=currency)
    db.add(settlement)
    db.flush()
    transfers = optimize_settlement(balance_data, currency)
    for item in transfers:
        db.add(Transfer(settlement_id=settlement.id, from_user_id=item.from_user, to_user_id=item.to_user, amount_minor=item.amount_minor, currency=item.currency))
    audit(db, user.id, "settlement.proposed", "settlement", settlement.id)
    db.commit()
    return {"id": settlement.id, "status": settlement.status, "transfers": [{"from_user_id": item.from_user, "to_user_id": item.to_user, "amount_minor": item.amount_minor, "currency": item.currency} for item in transfers]}


@app.get("/api/v1/groups/{group_id}/settlements")
def list_settlements(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    require_member(db, group_id, user.id)
    settlements = db.scalars(select(Settlement).where(Settlement.group_id == group_id).order_by(Settlement.created_at.desc())).all()
    return [{"id": item.id, "status": item.status, "transfers": [{"id": transfer.id, "from_user_id": transfer.from_user_id, "to_user_id": transfer.to_user_id, "amount_minor": transfer.amount_minor, "status": transfer.status} for transfer in item.transfers]} for item in settlements]


@app.post("/api/v1/settlements/{settlement_id}/approve")
def approve_settlement(settlement_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "Settlement not found")
    require_member(db, settlement.group_id, user.id)
    if settlement.status not in {"proposed", "recalculated"}:
        raise HTTPException(409, "Settlement is not awaiting approval")
    settlement.status = "approved"
    audit(db, user.id, "settlement.approved", "settlement", settlement.id)
    db.commit()
    return {"id": settlement.id, "status": settlement.status}


@app.post("/api/v1/settlements/{settlement_id}/recalculate")
def recalculate_settlement(settlement_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "Settlement not found")
    require_member(db, settlement.group_id, user.id)
    balance_data = balances(settlement.group_id, user, db)["balances"]
    settlement.status = "recalculated"
    transfers = optimize_settlement(balance_data, settlement.currency)
    for item in transfers:
        db.add(Transfer(settlement_id=settlement.id, from_user_id=item.from_user, to_user_id=item.to_user, amount_minor=item.amount_minor, currency=item.currency))
    audit(db, user.id, "settlement.recalculated", "settlement", settlement.id)
    db.commit()
    return {"id": settlement.id, "status": settlement.status, "transfer_count": len(transfers)}


@app.post("/api/v1/transfers/{transfer_id}/payment-order")
def create_payment_order(transfer_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    transfer = db.get(Transfer, transfer_id)
    if not transfer:
        raise HTTPException(404, "Transfer not found")
    require_member(db, transfer.settlement.group_id, user.id)
    if transfer.from_user_id != user.id:
        raise HTTPException(403, "Only the debtor can authorize this payment")
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key is required")
    from app.models import IdempotencyKey
    previous = db.get(IdempotencyKey, idempotency_key)
    if previous:
        return previous.response_json
    order = PayPalClient(settings).create_order(transfer.amount_minor, transfer.currency, str(request.url) + "/return", str(request.url) + "/cancel")
    payment = Payment(transfer_id=transfer.id, amount_minor=transfer.amount_minor, currency=transfer.currency, provider_order_id=order["id"], status="APPROVAL_PENDING")
    db.add(payment)
    db.flush()
    result = {"payment_id": payment.id, "order_id": order["id"], "approval_url": next((link["href"] for link in order.get("links", []) if link.get("rel") == "approve"), None), "status": payment.status}
    db.add(IdempotencyKey(key=idempotency_key, user_id=user.id, response_json=result))
    db.commit()
    return result


@app.post("/api/v1/payments/{payment_id}/capture")
def capture_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    require_member(db, payment.transfer.settlement.group_id, user.id)
    if payment.transfer.from_user_id != user.id:
        raise HTTPException(403, "Only the debtor can authorize this payment")
    if payment.status not in {"APPROVAL_PENDING", "CAPTURE_PENDING"}:
        raise HTTPException(409, "Payment is not ready for capture")
    payment.status = "CAPTURE_PENDING"
    result = PayPalClient(settings).capture_order(payment.provider_order_id or "")
    payment.provider_capture_id = result.get("purchase_units", [{}])[0].get("payments", {}).get("captures", [{}])[0].get("id")
    if result.get("status") == "COMPLETED":
        payment.status = "COMPLETED"
        payment.transfer.status = "completed"
    db.commit()
    return {"id": payment.id, "status": payment.status, "provider_status": result.get("status")}


@app.get("/api/v1/payments/{payment_id}")
def get_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    require_member(db, payment.transfer.settlement.group_id, user.id)
    return {"id": payment.id, "status": payment.status, "order_id": payment.provider_order_id, "amount_minor": payment.amount_minor, "currency": payment.currency}


@app.post("/api/v1/webhooks/paypal")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    body = await request.json()
    headers = {key.upper(): value for key, value in request.headers.items()}
    if not settings.paypal_webhook_id:
        raise HTTPException(503, "PayPal webhook verification is not configured")
    verified = PayPalClient(settings).verify_webhook(headers, body)
    event_id = body.get("id")
    if not verified or not event_id:
        raise HTTPException(400, "Invalid PayPal webhook")
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id == event_id))
    if existing:
        return {"status": "duplicate"}
    event = WebhookEvent(provider_event_id=event_id, event_type=body.get("event_type", "unknown"), payload=body, verified=True, processed=False)
    db.add(event)
    resource = body.get("resource", {})
    order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id") or resource.get("id")
    payment = db.scalar(select(Payment).where(Payment.provider_order_id == order_id))
    if payment and body.get("event_type") in {"PAYMENT.CAPTURE.COMPLETED", "CHECKOUT.ORDER.COMPLETED"}:
        payment.status = "COMPLETED"
        payment.transfer.status = "completed"
        event.processed = True
    db.commit()
    return {"status": "processed" if event.processed else "accepted"}


@app.post("/api/v1/agent/chat")
def agent_chat(payload: ChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    result = orchestrator.chat(payload.message, payload.context)
    audit(db, user.id, "agent.run", "agent", user.id, {"role": result.role, "tool_calls": result.tool_calls})
    db.commit()
    return {"role": result.role, "message": result.message, "tool_calls": result.tool_calls}
