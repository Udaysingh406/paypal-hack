import base64
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings
from app.core.currencies import normalize_currency


class PayPalConfigurationError(RuntimeError):
    """Raised when local Sandbox configuration is incomplete."""


def validate_order_response(order: dict[str, Any], amount_minor: int, currency: str) -> str:
    if not isinstance(order, dict) or not order.get("id"):
        raise ValueError("provider response is missing an order id")
    units = order.get("purchase_units")
    amount = units[0].get("amount") if isinstance(units, list) and units else None
    if not isinstance(amount, dict) or amount.get("currency_code") != normalize_currency(currency):
        raise ValueError("provider response has an invalid currency")
    try:
        provider_value = Decimal(str(amount["value"]))
        if provider_value != provider_value.quantize(Decimal("0.01")):
            raise ValueError("provider response has fractional minor units")
        provider_minor = int(provider_value * Decimal(100))
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise ValueError("provider response has an invalid amount") from exc
    if provider_minor != amount_minor:
        raise ValueError("provider response amount does not match reservation")
    return str(order["id"])


class PayPalClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _credentials_configured(self) -> None:
        if not self.settings.paypal_client_id or not self.settings.paypal_client_secret.get_secret_value():
            raise PayPalConfigurationError("PayPal Sandbox credentials are not configured")

    def access_token(self) -> str:
        self._credentials_configured()
        encoded = base64.b64encode(f"{self.settings.paypal_client_id}:{self.settings.paypal_client_secret.get_secret_value()}".encode()).decode()
        response = httpx.post(f"{self.settings.paypal_base_url}/v1/oauth2/token", headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"}, data="grant_type=client_credentials", timeout=20)
        response.raise_for_status()
        return response.json()["access_token"]

    def create_order(self, amount_minor: int, currency: str, return_url: str, cancel_url: str, request_id: str | None = None) -> dict[str, Any]:
        if amount_minor <= 0:
            raise ValueError("payment amount must be positive")
        currency = normalize_currency(currency)
        token = self.access_token()
        amount = Decimal(amount_minor) / Decimal(100)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        response = httpx.post(f"{self.settings.paypal_base_url}/v2/checkout/orders", headers=headers, json={"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": currency, "value": f"{amount:.2f}"}}], "application_context": {"return_url": return_url, "cancel_url": cancel_url}}, timeout=20)
        response.raise_for_status()
        return response.json()
    def get_order(self, order_id: str) -> dict[str, Any]:
        token = self.access_token()
        response = httpx.get(f"{self.settings.paypal_base_url}/v2/checkout/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
        response.raise_for_status()
        return response.json()

    def capture_order(self, order_id: str, request_id: str | None = None) -> dict[str, Any]:
        token = self.access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        response = httpx.post(f"{self.settings.paypal_base_url}/v2/checkout/orders/{order_id}/capture", headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    def verify_webhook(self, headers: dict[str, str], body: dict[str, Any]) -> bool:
        token = self.access_token()
        payload = {"auth_algo": headers.get("PAYPAL-AUTH-ALGO"), "cert_url": headers.get("PAYPAL-CERT-URL"), "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"), "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"), "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"), "webhook_id": self.settings.paypal_webhook_id, "webhook_event": body}
        response = httpx.post(f"{self.settings.paypal_base_url}/v1/notifications/verify-webhook-signature", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload, timeout=20)
        response.raise_for_status()
        return response.json().get("verification_status") == "SUCCESS"


def validate_capture_response(result: dict[str, Any], amount_minor: int, currency: str) -> tuple[str, str]:
    if not isinstance(result, dict) or result.get("status") not in {"COMPLETED", "PENDING"}:
        raise ValueError("provider capture response is not successful or pending")
    units = result.get("purchase_units")
    purchase_unit = units[0] if isinstance(units, list) and units else None
    captures = purchase_unit.get("payments", {}).get("captures", []) if isinstance(purchase_unit, dict) else []
    capture = captures[0] if isinstance(captures, list) and captures else None
    if not isinstance(capture, dict) or not capture.get("id"):
        raise ValueError("provider capture response is missing a capture id")
    amount = capture.get("amount")
    if not isinstance(amount, dict) or amount.get("currency_code") != normalize_currency(currency):
        raise ValueError("provider capture response has an invalid currency")
    try:
        provider_minor = int(Decimal(str(amount["value"])) * Decimal(100))
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise ValueError("provider capture response has an invalid amount") from exc
    if provider_minor != amount_minor:
        raise ValueError("provider capture response amount does not match payment")
    return str(capture["id"]), str(capture.get("status") or result["status"])
