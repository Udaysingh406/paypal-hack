# PayPal Sandbox

The `PayPalSandboxAdapter` is intentionally a network boundary. It validates
positive integer minor-unit amounts and does not expose client secrets. The MVP
does not claim that a standard Orders API checkout is peer-to-peer or escrow;
it charges the configured merchant account. Sandbox credentials and webhook
verification must be configured before external payment tests can run.
