import hashlib
import json
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import create_token, get_current_user, hash_password, verify_password
from app.core.config import get_settings
from app.core.currencies import normalize_currency
from app.db import Base, engine, get_db
from app.models import (
    CaptureAttempt,
    Expense,
    ExpenseSplit,
    Group,
    GroupMember,
    Payment,
    PaymentAttempt,
    Settlement,
    Transfer,
    User,
    WebhookEvent,
)
from app.schemas import (
    ChatRequest,
    ExpenseCreate,
    GroupCreate,
    LoginRequest,
    MemberCreate,
    UserCreate,
)
from app.services.agent import AgentOrchestrator
from app.services.capture_attempts import (
    complete_metadata_capture,
    mark_capture,
    reconcile_capture,
    reserve_capture,
)
from app.services.groups import audit, require_admin, require_member
from app.services.idempotency import (
    archived_key_exists,
    validate_new_payment_integrity,
    raise_recovery_required,
    validate_active_replay,
    validate_payment_integrity,
)
from app.services.payment_attempts import (
    is_expected_reservation_conflict,
    mark_attempt,
    matches_reservation,
    reserve_payment_order,
)
from app.services.payments import (
    PayPalClient,
    PayPalConfigurationError,
    validate_capture_response,
    validate_order_response,
)
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
    return {"id": expense.id, "amount_minor": expense.amount_minor, "currency": expense.currency, "status": expense.status}


@app.get("/api/v1/groups/{group_id}/expenses")
def list_expenses(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    require_member(db, group_id, user.id)
    expenses = db.scalars(select(Expense).where(Expense.group_id == group_id).order_by(Expense.created_at.desc())).all()
    return [{"id": item.id, "description": item.description, "amount_minor": item.amount_minor, "currency": item.currency, "paid_by_id": item.paid_by_id, "status": item.status} for item in expenses]


@app.delete("/api/v1/expenses/{expense_id}", status_code=204)
def delete_expense(expense_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    expense = db.get(Expense, expense_id)
    if not expense:
        raise HTTPException(404, "Expense not found")
    require_member(db, expense.group_id, user.id)
    membership = db.scalar(
        select(GroupMember).where(
            GroupMember.group_id == expense.group_id,
            GroupMember.user_id == user.id,
        )
    )
    if expense.paid_by_id != user.id and (not membership or membership.role not in {"owner", "admin"}):
        raise HTTPException(403, "Only the payer or a group administrator can delete this expense")
    if expense.status == "deleted":
        raise HTTPException(409, "Expense is already deleted")
    if db.scalar(select(Settlement.id).where(Settlement.group_id == expense.group_id, Settlement.status == "approved")):
        raise HTTPException(409, "Expense is locked by an approved settlement")
    expense.status = "deleted"
    audit(db, user.id, "expense.deleted", "expense", expense.id)
    db.commit()


def _active_expenses_and_currency(db: Session, group_id: str) -> tuple[list[Expense], str | None]:
    expenses = db.scalars(
        select(Expense).where(Expense.group_id == group_id, Expense.status == "active")
    ).all()
    currencies = {normalize_currency(expense.currency) for expense in expenses}
    if len(currencies) > 1:
        raise HTTPException(
            status_code=409,
            detail="Active expenses use multiple currencies; settle each currency separately",
        )
    return expenses, next(iter(currencies), None)


def _group_balances(db: Session, group_id: str) -> tuple[dict[str, int], str | None]:
    members = db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id)).all()
    contributions = {member: 0 for member in members}
    shares = {member: 0 for member in members}
    expenses, currency = _active_expenses_and_currency(db, group_id)
    for expense in expenses:
        contributions[expense.paid_by_id] += expense.amount_minor
        for split in expense.splits:
            shares[split.user_id] += split.amount_minor
    return calculate_balances(contributions, shares), currency


@app.get("/api/v1/groups/{group_id}/balances")
def balances(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    require_member(db, group_id, user.id)
    balance_data, currency = _group_balances(db, group_id)
    return {"currency": currency, "balances": balance_data}


@app.post("/api/v1/groups/{group_id}/settlements/optimize", status_code=201)
def optimize(group_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    require_member(db, group_id, user.id)
    db.scalar(select(Group).where(Group.id == group_id).with_for_update())
    balance_data, currency = _group_balances(db, group_id)
    if currency is None:
        raise HTTPException(400, "At least one active expense is required to create a settlement")
    settlement = Settlement(group_id=group_id, currency=normalize_currency(currency))
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
    require_admin(db, settlement.group_id, user.id)
    db.scalar(select(Group).where(Group.id == settlement.group_id).with_for_update())
    settlement = db.scalar(select(Settlement).where(Settlement.id == settlement_id).with_for_update())
    if settlement.status not in {"proposed", "recalculated"}:
        raise HTTPException(409, "Settlement is not awaiting approval")
    blocking = db.scalar(
        select(Settlement.id)
        .join(Transfer, Transfer.settlement_id == Settlement.id)
        .outerjoin(Payment, Payment.transfer_id == Transfer.id)
        .where(
            Settlement.group_id == settlement.group_id,
            Settlement.status == "approved",
            Settlement.id != settlement.id,
            or_(
                and_(
                    Transfer.status == "pending",
                    or_(
                        Payment.id.is_(None),
                        Payment.status.not_in({"COMPLETED", "FAILED", "CANCELLED"}),
                    ),
                ),
            ),
        )
    )
    if blocking:
        raise HTTPException(409, "Another approved settlement has outstanding payment obligations")
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
    db.scalar(select(Group).where(Group.id == settlement.group_id).with_for_update())
    settlement = db.scalar(select(Settlement).where(Settlement.id == settlement_id).with_for_update())
    if settlement.status == "approved":
        raise HTTPException(409, "Approved settlements are immutable and cannot be recalculated")
    balance_data, currency = _group_balances(db, settlement.group_id)
    if currency is None:
        raise HTTPException(400, "At least one active expense is required to recalculate a settlement")
    if currency != settlement.currency:
        raise HTTPException(409, "Settlement currency does not match the group's active expenses")
    settlement.status = "recalculated"
    transfers = optimize_settlement(balance_data, settlement.currency)
    existing = db.scalars(
        select(Transfer)
        .where(Transfer.settlement_id == settlement.id)
        .with_for_update()
    ).all()
    reusable: dict[tuple[str, str, int, str], list[Transfer]] = {}
    fulfilled: set[tuple[str, str, int, str]] = set()
    for transfer in existing:
        transfer_key = (
            transfer.from_user_id,
            transfer.to_user_id,
            transfer.amount_minor,
            transfer.currency,
        )
        if transfer.status == "completed" or (
            transfer.payment is not None and transfer.payment.status == "COMPLETED"
        ):
            fulfilled.add(transfer_key)
            continue
        if transfer.status == "pending" and transfer.payment is None:
            reusable.setdefault(transfer_key, []).append(transfer)
        elif transfer.status == "pending" and transfer.payment is not None:
            payment = db.scalar(select(Payment).where(Payment.id == transfer.payment.id).with_for_update())
            order_attempts = db.scalars(
                select(PaymentAttempt)
                .where(PaymentAttempt.transfer_id == transfer.id)
                .with_for_update()
            ).all()
            capture_attempts = db.scalars(
                select(CaptureAttempt)
                .where(CaptureAttempt.transfer_id == transfer.id)
                .with_for_update()
            ).all()
            active_order = any(item.status in {"RESERVED", "PROVIDER_REQUESTED", "PERSISTENCE_PENDING", "RECONCILIATION_REQUIRED"} or item.provider_order_id for item in order_attempts)
            active_capture = any(item.status in {"RESERVED", "PROVIDER_REQUESTED", "METADATA_PERSISTED", "PERSISTENCE_PENDING", "RECONCILIATION_REQUIRED", "COMPLETED"} or item.provider_capture_id for item in capture_attempts)
            if payment.provider_order_id or payment.provider_capture_id or active_order or active_capture or payment.status not in {"FAILED", "CANCELLED"}:
                raise HTTPException(409, "Settlement has a provider-backed payment obligation requiring recovery")
            transfer.status = "superseded"
    for item in transfers:
        key = (item.from_user, item.to_user, item.amount_minor, item.currency)
        if key in fulfilled:
            continue
        matches = reusable.get(key, [])
        if matches:
            matches.pop()
        else:
            db.add(
                Transfer(
                    settlement_id=settlement.id,
                    from_user_id=item.from_user,
                    to_user_id=item.to_user,
                    amount_minor=item.amount_minor,
                    currency=item.currency,
                )
            )
    for unmatched in (transfer for matches in reusable.values() for transfer in matches):
        unmatched.status = "superseded"
    audit(db, user.id, "settlement.recalculated", "settlement", settlement.id)
    db.commit()
    return {"id": settlement.id, "status": settlement.status, "transfer_count": len(transfers)}


@app.post("/api/v1/transfers/{transfer_id}/payment-order")
def create_payment_order(transfer_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    transfer = db.scalar(select(Transfer).where(Transfer.id == transfer_id).with_for_update())
    if not transfer:
        raise HTTPException(404, "Transfer not found")
    require_member(db, transfer.settlement.group_id, user.id)
    if transfer.from_user_id != user.id:
        raise HTTPException(403, "Only the debtor can authorize this payment")
    if transfer.settlement.status != "approved":
        raise HTTPException(409, "Settlement must be approved before creating a payment order")
    if transfer.status != "pending":
        raise HTTPException(409, "Transfer is not pending")
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key is required")
    if len(idempotency_key) > 160:
        raise HTTPException(400, "Idempotency-Key is too long")
    from app.models import IdempotencyKey
    operation = "payment-order"
    resource_id = transfer.id
    previous = db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key))
    if previous and previous.user_id != user.id:
        raise HTTPException(409, "Idempotency-Key belongs to another user")
    if previous and previous.operation == "legacy":
        legacy_payment_id = previous.response_json.get("payment_id") if isinstance(previous.response_json, dict) else None
        legacy_payment = db.get(Payment, legacy_payment_id) if legacy_payment_id else None
        validate_payment_integrity(legacy_payment, transfer, user.id)
        return previous.response_json
    if not previous and transfer.payment:
        validate_payment_integrity(transfer.payment, transfer, user.id)
        raise_recovery_required()
    if not previous and archived_key_exists(db, idempotency_key):
        raise_recovery_required()
    fingerprint = hashlib.sha256(json.dumps({
        "transfer_id": transfer.id,
        "amount_minor": transfer.amount_minor,
        "currency": transfer.currency,
        "return_url": str(request.url) + "/return",
        "cancel_url": str(request.url) + "/cancel",
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if previous:
        return validate_active_replay(db, previous, transfer, user.id, operation, resource_id, fingerprint)
    return_url, cancel_url = str(request.url) + "/return", str(request.url) + "/cancel"
    amount_minor, currency = transfer.amount_minor, transfer.currency
    try:
        attempt = reserve_payment_order(db, transfer_id=transfer.id, user_id=user.id, idempotency_key=idempotency_key, request_fingerprint=fingerprint, amount_minor=transfer.amount_minor, currency=currency, return_url=return_url, cancel_url=cancel_url)
    except IntegrityError as exc:
        if not is_expected_reservation_conflict(exc):
            db.rollback()
            raise
        db.rollback()
        winning_attempt = db.scalar(select(PaymentAttempt).where(
            PaymentAttempt.transfer_id == transfer.id,
            PaymentAttempt.operation == operation,
        ))
        if winning_attempt is None:
            raise HTTPException(409, "Payment attempt conflict; retry safely") from exc
        if not matches_reservation(winning_attempt, user_id=user.id, idempotency_key=idempotency_key, fingerprint=fingerprint, amount_minor=amount_minor, currency=currency, return_url=return_url, cancel_url=cancel_url):
            raise HTTPException(409, "Payment attempt conflict") from exc
        raise_recovery_required()
    if not matches_reservation(attempt, user_id=user.id, idempotency_key=idempotency_key, fingerprint=fingerprint, amount_minor=amount_minor, currency=currency, return_url=return_url, cancel_url=cancel_url):
        raise HTTPException(409, "Payment attempt belongs to another request")
    if attempt.status != "RESERVED":
        raise_recovery_required()
    attempt_id, provider_request_id = attempt.id, attempt.provider_request_id
    try:
        attempt.status = "PROVIDER_REQUESTED"
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, "Payment persistence unavailable") from exc
    try:
        order = PayPalClient(settings).create_order(amount_minor, currency, return_url, cancel_url, request_id=provider_request_id)
    except Exception as exc:
        if isinstance(exc, PayPalConfigurationError):
            classification = "provider_configuration_error"
            status = "FAILED"
        else:
            classification = "timeout" if isinstance(exc, TimeoutError) else "provider_rejected" if exc.__class__.__name__ == "HTTPStatusError" else "provider_uncertain"
            status = "RECONCILIATION_REQUIRED" if classification != "provider_rejected" else "FAILED"
        mark_attempt(db, attempt_id, status, error_classification=classification)
        if classification == "provider_configuration_error":
            raise HTTPException(503, "PayPal Sandbox configuration is unavailable") from exc
        if status == "FAILED":
            raise HTTPException(502, "Payment provider rejected the order") from exc
        raise HTTPException(503, "Payment provider outcome requires reconciliation") from exc
    try:
        order_id = validate_order_response(order, amount_minor, currency)
    except ValueError:
        mark_attempt(db, attempt_id, "RECONCILIATION_REQUIRED", error_classification="malformed_provider_response", provider_order_id=order.get("id") if isinstance(order, dict) else None)
        raise HTTPException(409, "Payment recovery required")
    mark_attempt(db, attempt_id, "PERSISTENCE_PENDING", provider_order_id=order_id)
    payment = Payment(transfer_id=transfer_id, amount_minor=amount_minor, currency=currency, provider_order_id=order_id, status="APPROVAL_PENDING")
    validate_new_payment_integrity(payment, transfer)
    db.add(payment)
    db.flush()
    result = {"payment_id": payment.id, "order_id": order_id, "approval_url": next((link["href"] for link in order.get("links", []) if link.get("rel") == "approve"), None), "status": payment.status}
    db.add(IdempotencyKey(
        key=idempotency_key,
        user_id=user.id,
        operation=operation,
        resource_id=resource_id,
        request_fingerprint=fingerprint,
        response_json=result,
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key))
        if not previous:
            raise HTTPException(409, "Idempotency-Key could not be recorded")
        if previous.user_id != user.id:
            raise HTTPException(409, "Idempotency-Key belongs to another user")
        if previous.operation != operation or previous.resource_id != resource_id:
            raise HTTPException(409, "Idempotency-Key was already used for another resource or operation")
        if previous.request_fingerprint != fingerprint:
            raise HTTPException(409, "Idempotency-Key was already used with different parameters")
        return previous.response_json
    except SQLAlchemyError as exc:
        db.rollback()
        mark_attempt(db, attempt_id, "PERSISTENCE_PENDING", error_classification=type(exc).__name__[:64])
        raise HTTPException(503, "Payment persistence unavailable") from exc
    try:
        mark_attempt(db, attempt_id, "COMPLETED")
    except SQLAlchemyError as exc:
        db.rollback()
        mark_attempt(db, attempt_id, "PERSISTENCE_PENDING", error_classification=type(exc).__name__[:64])
        raise HTTPException(503, "Payment persistence unavailable") from exc
    return result


@app.post("/api/v1/payments/{payment_id}/capture")
def capture_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())
    if not payment:
        raise HTTPException(404, "Payment not found")
    require_member(db, payment.transfer.settlement.group_id, user.id)
    if payment.transfer.from_user_id != user.id:
        raise HTTPException(403, "Only the debtor can authorize this payment")
    if payment.transfer.status != "pending" or payment.status != "APPROVAL_PENDING":
        raise HTTPException(409, "Payment is not ready for capture")
    validate_payment_integrity(payment, payment.transfer, user.id)
    if not payment.provider_order_id or payment.provider_capture_id:
        raise_recovery_required()
    try:
        attempt = reserve_capture(db, payment, payment.transfer)
    except RuntimeError as exc:
        raise HTTPException(409, "Payment recovery required") from exc
    if attempt.status == "COMPLETED":
        return {"id": payment.id, "status": "COMPLETED", "provider_status": attempt.provider_capture_status}
    if attempt.status == "METADATA_PERSISTED":
        capture_id = attempt.provider_capture_id
        provider_status = attempt.provider_capture_status or "COMPLETED"
    else:
        try:
            mark_capture(db, attempt.id, "PROVIDER_REQUESTED")
            result = PayPalClient(settings).capture_order(attempt.provider_order_id, request_id=attempt.provider_request_id)
        except Exception as exc:
            db.rollback()
            try:
                mark_capture(db, attempt.id, "RECONCILIATION_REQUIRED", reconciliation_state="REQUIRED", error_classification=type(exc).__name__[:64])
            except Exception:  # noqa: BLE001
                db.rollback()
            raise HTTPException(503, "Payment recovery required") from exc
        try:
            capture_id, provider_status = validate_capture_response(result, attempt.amount_minor, attempt.currency)
            if provider_status != "COMPLETED":
                mark_capture(db, attempt.id, "RECONCILIATION_REQUIRED", reconciliation_state="REQUIRED", provider_capture_id=capture_id, provider_capture_status=provider_status)
                raise HTTPException(409, "Payment recovery required")
            mark_capture(db, attempt.id, "METADATA_PERSISTED", provider_capture_id=capture_id, provider_capture_status=provider_status)
        except HTTPException:
            raise
        except Exception as exc:
            db.rollback()
            try:
                mark_capture(db, attempt.id, "RECONCILIATION_REQUIRED", reconciliation_state="REQUIRED", error_classification="malformed_provider_response")
            except Exception:  # noqa: BLE001
                db.rollback()
            raise HTTPException(409, "Payment recovery required") from exc
    if not capture_id:
        raise HTTPException(409, "Payment recovery required")
    try:
        payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())
        if not payment or payment.provider_capture_id not in {None, capture_id}:
            raise RuntimeError("payment capture association conflict")
        payment.provider_capture_id = capture_id
        payment.status = "COMPLETED"
        payment.transfer.status = "completed"
        mark_capture(db, attempt.id, "COMPLETED", reconciliation_state="RESOLVED")
    except Exception as exc:
        db.rollback()
        raise HTTPException(503, "Payment completion requires recovery") from exc
    return {"id": payment.id, "status": payment.status, "provider_status": provider_status}


@app.post("/api/v1/payments/{payment_id}/capture/reconcile")
def reconcile_payment_capture(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    payment = db.scalar(select(Payment).where(Payment.id == payment_id))
    if not payment:
        raise HTTPException(404, "Payment not found")
    require_member(db, payment.transfer.settlement.group_id, user.id)
    if payment.transfer.from_user_id != user.id:
        raise HTTPException(403, "Only the debtor can recover this payment")
    attempt = db.scalar(select(CaptureAttempt).where(CaptureAttempt.payment_id == payment.id).order_by(CaptureAttempt.created_at.desc()))
    if attempt is None:
        raise_recovery_required()
    try:
        recovered, _ = reconcile_capture(db, attempt.id, lambda order_id: PayPalClient(settings).get_order(order_id))
    except RuntimeError as exc:
        raise HTTPException(409, "Payment recovery remains blocked") from exc
    return {"id": recovered.id, "status": recovered.status, "provider_status": "COMPLETED"}


@app.get("/api/v1/payments/{payment_id}")
def get_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    group_id = payment.transfer.settlement.group_id
    require_member(db, group_id, user.id)
    membership = db.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user.id,
        )
    )
    response = {
        "id": payment.id,
        "status": payment.status,
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
    }
    if payment.transfer.from_user_id == user.id or (membership and membership.role in {"owner", "admin"}):
        response["order_id"] = payment.provider_order_id
    return response


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
    existing = db.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.provider_event_id == event_id)
        .with_for_update()
    )
    if existing:
        return {"status": "duplicate"}
    event_type = body.get("event_type")
    if event_type not in {"PAYMENT.CAPTURE.COMPLETED", "CHECKOUT.ORDER.COMPLETED"}:
        raise HTTPException(400, "Unsupported PayPal event")
    try:
        resource = body["resource"]
        if not isinstance(resource, dict):
            raise TypeError
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            order_id = resource["supplementary_data"]["related_ids"]["order_id"]
            capture_id = resource["id"]
            capture_status = resource["status"]
            amount = resource["amount"]
        else:
            order_id = resource["id"]
            purchase_units = resource["purchase_units"]
            capture = purchase_units[0]["payments"]["captures"][0]
            capture_id = capture["id"]
            capture_status = capture["status"]
            amount = capture["amount"]
        if capture_status != "COMPLETED" or not order_id or not capture_id or not isinstance(amount, dict):
            raise ValueError
        event_currency = normalize_currency(amount["currency_code"])
        event_value = Decimal(str(amount["value"]))
        if event_value != event_value.quantize(Decimal("0.01")):
            raise ValueError
        event_amount_minor = int(event_value * Decimal(100))
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(400, "Invalid or incomplete PayPal capture event") from exc

    attempt = db.scalar(
        select(CaptureAttempt)
        .where(CaptureAttempt.provider_order_id == str(order_id))
        .where((CaptureAttempt.provider_capture_id.is_(None)) | (CaptureAttempt.provider_capture_id == str(capture_id)))
        .order_by(CaptureAttempt.created_at.desc())
        .with_for_update()
    )
    if attempt is None:
        raise HTTPException(409, "PayPal capture is not associated with a recoverable payment")
    payment = db.scalar(select(Payment).where(Payment.id == attempt.payment_id).with_for_update())
    transfer = db.scalar(select(Transfer).where(Transfer.id == attempt.transfer_id).with_for_update())
    if payment is None or transfer is None or payment.transfer_id != transfer.id:
        raise HTTPException(409, "PayPal capture association is invalid")
    if payment.provider_order_id != str(order_id) or payment.amount_minor != event_amount_minor or transfer.amount_minor != event_amount_minor or payment.currency != event_currency or transfer.currency != event_currency:
        raise HTTPException(409, "PayPal capture financial data conflicts with local records")
    if payment.provider_capture_id not in {None, str(capture_id)} or attempt.provider_capture_id not in {None, str(capture_id)}:
        raise HTTPException(409, "PayPal capture conflicts with an existing capture")
    event = WebhookEvent(provider_event_id=event_id, event_type=event_type, payload=body, verified=True, processed=False)
    db.add(event)
    if payment.status == "COMPLETED":
        if payment.provider_capture_id != str(capture_id) or attempt.status != "COMPLETED":
            db.rollback()
            raise HTTPException(409, "Completed Payment conflicts with PayPal capture")
        event.processed = True
    else:
        if attempt.status not in {"PROVIDER_REQUESTED", "RECONCILIATION_REQUIRED", "METADATA_PERSISTED"} or transfer.status != "pending":
            db.rollback()
            raise HTTPException(409, "PayPal capture is not in a recoverable state")
        attempt.provider_capture_id = str(capture_id)
        attempt.provider_capture_status = "COMPLETED"
        attempt.status = "METADATA_PERSISTED"
        attempt.reconciliation_state = "RESOLVED"
        complete_metadata_capture(db, attempt.id)
        event.processed = True
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate = db.scalar(
            select(WebhookEvent).where(WebhookEvent.provider_event_id == event_id)
        )
        if duplicate:
            return {"status": "duplicate"}
        raise
    return {"status": "processed" if event.processed else "accepted"}


@app.post("/api/v1/agent/chat")
def agent_chat(payload: ChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    result = orchestrator.chat(payload.message, payload.context)
    audit(db, user.id, "agent.run", "agent", user.id, {"role": result.role, "tool_calls": result.tool_calls})
    db.commit()
    return {"role": result.role, "message": result.message, "tool_calls": result.tool_calls}
