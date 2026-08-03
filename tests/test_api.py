from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_plan_matches_demo_budget() -> None:
    response = client.post(
        "/api/v1/workflows/plan",
        json={
            "brief": "Кофейня в Казани: запустить кампанию для утренних гостей.",
            "budget_rub": 250,
            "priority": "balanced",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["required_cost_rub"] == 57
    assert body["maximum_cost_rub"] == 177
    assert body["steps"][-1]["requires_approval"] is True


def test_execute_stops_at_approval_gate() -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json={
            "brief": "Кофейня в Казани: запустить кампанию для утренних гостей.",
            "budget_rub": 250,
        },
    ).json()
    response = client.post(f"/api/v1/workflows/{planned['id']}/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["steps"][-1]["status"] == "awaiting_approval"
    assert body["remaining_budget_rub"] == 193


def test_low_budget_is_rejected_without_spend() -> None:
    response = client.post(
        "/api/v1/workflows/plan",
        json={
            "brief": "Кофейня в Казани: запустить кампанию для утренних гостей.",
            "budget_rub": 30,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "budget_too_low"
