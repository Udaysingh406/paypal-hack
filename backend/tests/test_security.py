import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_production_settings_require_a_strong_auth_secret() -> None:
    with pytest.raises(ValidationError, match="AUTH_SECRET"):
        Settings(app_env="production", auth_secret="short")


def test_missing_app_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(ValidationError, match="app_env|APP_ENV"):
        Settings(auth_secret="x" * 32)


def test_missing_auth_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    with pytest.raises(ValidationError, match="auth_secret|AUTH_SECRET"):
        Settings(app_env="production")


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("secret", ["change-me-in-development", "weak"])
def test_non_test_environments_reject_default_and_weak_secrets(environment: str, secret: str) -> None:
    with pytest.raises(ValidationError, match="AUTH_SECRET"):
        Settings(app_env=environment, auth_secret=secret)


def test_valid_production_configuration_is_accepted() -> None:
    settings = Settings(app_env="production", auth_secret="production-secret-" + "x" * 48)
    assert settings.app_env == "production"


def test_explicit_test_configuration_is_accepted() -> None:
    settings = Settings(app_env="test", auth_secret="test-secret-" + "x" * 48)
    assert settings.app_env == "test"
