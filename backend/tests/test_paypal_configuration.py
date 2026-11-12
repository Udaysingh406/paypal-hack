"""PayPal configuration tests; no provider or database access."""

import pytest
from app.core.config import Settings
from app.services.payments import PayPalClient, PayPalConfigurationError


def test_missing_paypal_credentials_is_a_local_configuration_error() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_secret="test-secret-" + "x" * 48,
        paypal_client_id="",
        paypal_client_secret="",
    )
    with pytest.raises(PayPalConfigurationError, match="not configured"):
        PayPalClient(settings)._credentials_configured()


def test_valid_paypal_configuration_is_accepted_without_network_access() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_secret="test-secret-" + "x" * 48,
        paypal_client_id="sandbox-client-id",
        paypal_client_secret="sandbox-client-secret",
    )
    PayPalClient(settings)._credentials_configured()
