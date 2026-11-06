# Architecture

SettleMate keeps deterministic financial rules in backend service modules.
FastAPI routes validate request shapes and delegate to services. The agent
orchestrator can select typed tools, but it does not receive database access or
permission to mutate financial records. PayPal is an external adapter boundary;
server-side verification is required before a payment can become `COMPLETED`.

The settlement engine uses integer minor units and a practical greedy
debtor-to-creditor matching algorithm. It validates that the input balances sum
to zero and emits only positive, non-self transfers.
