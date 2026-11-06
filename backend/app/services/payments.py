import base64
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings


class PayPalClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _credentials_configured(self) -> None:
        if not self.settings.paypal_client_id or not self.settings.paypal_client_secret.get_secret_value():
            raise RuntimeError("PayPal Sandbox credentials are not configured")

    def access_token(self) -> str:
        self._credentials_configured()
        encoded = base64.b64encode(f"{self.settings.paypal_client_id}:{self.settings.paypal_client_secret.get_secret_value()}".encode()).decode()
        response = httpx.post(f"{self.settings.paypal_base_url}/v1/oauth2/token", headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"}, data="grant_type=client_credentials", timeout=20)
        response.raise_for_status()
        return response.json()["access_token"]

    def create_order(self, amount_minor: int, currency: str, return_url: str, cancel_url: str) -> dict[str, Any]:
        if amount_minor <= 0:
            raise ValueError("payment amount must be positive")
        token = self.access_token()
        amount = Decimal(amount_minor) / Decimal(100)
        response = httpx.post(f"{self.settings.paypal_base_url}/v2/checkout/orders", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json={"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": currency, "value": f"{amount:.2f}"}}], "application_context": {"return_url": return_url, "cancel_url": cancel_url}}, timeout=20)
        response.raise_for_status()
        return response.json()

    def get_order(self, order_id: str) -> dict[str, Any]:
        token = self.access_token()
        response = httpx.get(f"{self.settings.paypal_base_url}/v2/checkout/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
        response.raise_for_status()
        return response.json()

    def capture_order(self, order_id: str) -> dict[str, Any]:
        token = self.access_token()
        response = httpx.post(f"{self.settings.paypal_base_url}/v2/checkout/orders/{order_id}/capture", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=20)
        response.raise_for_status()
        return response.json()

    def verify_webhook(self, headers: dict[str, str], body: dict[str, Any]) -> bool:
        token = self.access_token()
        payload = {"auth_algo": headers.get("PAYPAL-AUTH-ALGO"), "cert_url": headers.get("PAYPAL-CERT-URL"), "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"), "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"), "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"), "webhook_id": self.settings.paypal_webhook_id, "webhook_event": body}
        response = httpx.post(f"{self.settings.paypal_base_url}/v1/notifications/verify-webhook-signature", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload, timeout=20)
        response.raise_for_status()
        return response.json().get("verification_status") == "SUCCESS"
