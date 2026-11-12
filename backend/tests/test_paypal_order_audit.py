import httpx
import pytest
from scripts.audit_paypal_orders import AuditTarget, audit_target


def target() -> AuditTarget:
    return AuditTarget(1, "provider-order-test", "APPROVAL_PENDING", "pending", 1250, "INR", "verified")


def response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("GET", "https://paypal.test/order"))


def test_audit_classifies_matching_order_without_mutation_methods() -> None:
    class ReadOnlyClient:
        def get_order(self, order_id: str) -> dict:
            assert order_id == "provider-order-test"
            return {"status": "APPROVED", "purchase_units": [{"amount": {"value": "12.50", "currency_code": "INR"}}]}

        def create_order(self, *args, **kwargs):
            raise AssertionError("create_order must not be called")

        def capture_order(self, *args, **kwargs):
            raise AssertionError("capture_order must not be called")

    result = audit_target(target(), ReadOnlyClient().get_order)
    assert result.classification == "verified_terms"
    assert result.provider_status == "APPROVED"


def test_audit_classifies_404() -> None:
    def get_order(order_id: str) -> dict:
        raise httpx.HTTPStatusError("not found", request=httpx.Request("GET", "https://paypal.test/order"), response=response({}, 404))

    assert audit_target(target(), get_order).classification == "not_found"


def test_audit_classifies_timeout() -> None:
    def get_order(order_id: str) -> dict:
        raise httpx.TimeoutException("timeout")

    assert audit_target(target(), get_order).classification == "unavailable"


def test_audit_classifies_malformed_response() -> None:
    assert audit_target(target(), lambda order_id: {"status": "APPROVED"}).classification == "mismatch"
    assert audit_target(target(), lambda order_id: []).classification == "malformed"


@pytest.mark.parametrize("payload", [
    {"status": "APPROVED", "purchase_units": [{"amount": {"value": "12.51", "currency_code": "INR"}}]},
    {"status": "APPROVED", "purchase_units": [{"amount": {"value": "12.50", "currency_code": "USD"}}]},
])
def test_audit_classifies_amount_or_currency_mismatch(payload: dict) -> None:
    result = audit_target(target(), lambda order_id: payload)
    assert result.classification == "mismatch"
    assert not (result.amount_match and result.currency_match)
