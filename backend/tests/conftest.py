"""Explicit isolated settings for the backend test process."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AUTH_SECRET", "test-only-secret-" + "x" * 48)
