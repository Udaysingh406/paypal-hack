from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.main import app


def register(client: TestClient, email: str) -> tuple[str, str]:
    response = client.post("/api/v1/auth/register", json={"email": email, "display_name": email.split("@")[0], "password": "password123"})
    assert response.status_code == 201
    data = response.json()
    return data["access_token"], data["user_id"]


def test_group_access_is_scoped_to_members() -> None:
    test_id = uuid4().hex
    with TestClient(app) as client:
        token_a, user_a = register(client, f"a-{test_id}@example.com")
        token_b, _ = register(client, f"b-{test_id}@example.com")
        group = client.post("/api/v1/groups", headers={"Authorization": f"Bearer {token_a}"}, json={"name": "Private group"}).json()
        denied = client.get(f"/api/v1/groups/{group['id']}", headers={"Authorization": f"Bearer {token_b}"})
        assert denied.status_code == 404
        allowed = client.get(f"/api/v1/groups/{group['id']}", headers={"Authorization": f"Bearer {token_a}"})
        assert allowed.status_code == 200
        assert allowed.json()["members"][0]["id"] == user_a


def test_health_check_reports_database_status() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "settlemate-api", "database": "ok"}


def test_health_check_returns_service_unavailable_when_database_fails() -> None:
    def failing_db():
        class FailingSession:
            def execute(self, statement):
                raise OperationalError("SELECT 1", {}, RuntimeError("database down"))

        yield FailingSession()

    app.dependency_overrides[get_db] = failing_db
    try:
        with TestClient(app) as client:
            response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
