from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str
    database_url: str = "sqlite:///./settlemate.db"
    backend_cors_origins: str = "http://localhost:3000"
    auth_secret: SecretStr
    auth_token_ttl_seconds: int = 86400
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_webhook_id: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        environment = self.app_env.strip().lower()
        if not environment:
            raise ValueError("APP_ENV must be explicitly configured")
        secret = self.auth_secret.get_secret_value()
        if secret == "change-me-in-development" or len(secret) < 32:
            raise ValueError("AUTH_SECRET must be at least 32 characters and must not use the development default")
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.backend_cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
