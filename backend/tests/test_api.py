from fastapi.testclient import TestClient

from app.main import app


def register(client: TestClient, email: str) -> tuple[str, str]:
    response = client.post("/api/v1/auth/register", json={"email": email, "display_name": email.split("@")[0], "password": "password123"})
    assert response.status_code == 201
    data = response.json()
    return data["access_token"], data["user_id"]


def test_group_access_is_scoped_to_members() -> None:
    with TestClient(app) as client:
        token_a, user_a = register(client, "a@example.com")
        token_b, _ = register(client, "b@example.com")
        group = client.post("/api/v1/groups", headers={"Authorization": f"Bearer {token_a}"}, json={"name": "Private group"}).json()
        denied = client.get(f"/api/v1/groups/{group['id']}", headers={"Authorization": f"Bearer {token_b}"})
        assert denied.status_code == 404
        allowed = client.get(f"/api/v1/groups/{group['id']}", headers={"Authorization": f"Bearer {token_a}"})
        assert allowed.status_code == 200
        assert allowed.json()["members"][0]["id"] == user_a
