# API

`GET /health/live` and `GET /health/ready` provide process health without
returning configuration or credentials. `GET /api/v1/health` provides the
versioned service check.

`POST /api/v1/agent/chat` accepts `{ "message": "...", "context": {} }` and
returns a typed role, explanation, and bounded tool names. Tool execution is
still subject to backend validation and authorization.

Expense and settlement financial rules:

- Expense currency is normalized to uppercase three-letter form and money stays
  in integer minor units.
- The supported currency allowlist is shared by request schemas and service
  boundaries: `EUR`, `GBP`, `INR`, and `USD`. There is no implicit currency
  default.
- `GET /api/v1/groups/{group_id}/balances` and settlement generation return
  `409` when active expenses use multiple currencies.
- Expenses start in `active` state and are soft-deleted through the existing
  delete endpoint. Deletion is rejected when an approved settlement exists for
  the group.
- Payment orders require an approved settlement. Approved settlements cannot be
  recalculated. Recalculation reuses unchanged pending transfers, preserves
  completed/payment-backed transfers, and supersedes every obsolete pending
  transfer instead of deleting it. Superseded transfers are excluded from
  active obligations and payment-order creation.
- Only the expense payer or a group owner/admin can delete an expense.
- Approval, recalculation, payment-order creation, capture, and verified
  webhook application use PostgreSQL row locks. A group cannot have two
  overlapping approved settlement obligations; a later cycle can be approved
  after all prior transfers reach terminal states.
