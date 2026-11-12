# Architecture

SettleMate keeps deterministic financial rules in backend service modules.
FastAPI routes validate request shapes and delegate to services. The agent
orchestrator can select typed tools, but it does not receive database access or
permission to mutate financial records. PayPal is an external adapter boundary;
server-side verification is required before a payment can become `COMPLETED`.

The settlement engine uses integer minor units and a practical greedy
debtor-to-creditor matching algorithm. It validates that the input balances sum
to zero and emits only positive, non-self transfers.

Active expenses in one settlement must use one normalized three-letter currency.
The API rejects balance and settlement calculations for mixed-currency groups
instead of combining amounts. Newly created expenses are `active`; deletion is
a soft transition to `deleted`, restricted to the payer or a group
owner/admin, and is blocked once the group has an approved settlement.
Approved settlements are immutable. Recalculation locks the group, settlement,
and transfer rows where the database supports row locks, reuses unchanged
pending transfers without payments, preserves completed/payment-backed
transfers, and marks all obsolete pending transfers as `superseded`.
SQLite development mode ignores row-level locks; production PostgreSQL should
be used for concurrent recalculation and approval serialization. Approval
locks the group and rejects a second settlement while another approved
settlement has non-terminal transfers; once all transfers are terminal, the
next cycle is allowed.

Payment-order creation is serialized by the transfer row lock and an
idempotency key. The PayPal call must happen before the local payment row can
be committed, so an upstream order can become an unavoidable orphan if the
provider succeeds and the database commit or process fails afterward. The
order id is never silently reused for another transfer; operators must
reconcile such provider orders with PayPal. No destructive data migration is
required for this milestone: `superseded` is an additive transfer status and
existing rows, payment/order ids, and audit history are preserved. Apply the
existing Alembic chain before serving PostgreSQL traffic.
