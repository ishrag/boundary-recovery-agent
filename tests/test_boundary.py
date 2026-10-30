from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def reset_demo() -> None:
    response = client.post("/api/demo/reset")
    assert response.status_code == 200


def create_run() -> str:
    response = client.post("/api/runs", json={"scenario": "ack_lost"})
    assert response.status_code == 201
    return response.json()["run"]["id"]


def test_health_check() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_ack_lost_recovery_does_not_repeat_mutation() -> None:
    reset_demo()
    run_id = create_run()

    crashed = client.post(f"/api/runs/{run_id}/execute")
    assert crashed.status_code == 503
    assert crashed.json()["agent_knowledge"] == "OUTCOME_UNKNOWN"

    incident = client.get(f"/api/runs/{run_id}").json()
    assert incident["run"]["phase"] == "INCIDENT"
    assert incident["run"]["provider_mutation_requests"] == 1
    assert incident["run"]["provider_commits"] == 1
    assert len(incident["provider"]["effects"]) == 1

    recovered = client.post(f"/api/runs/{run_id}/recover")
    assert recovered.status_code == 200
    data = recovered.json()
    assert data["run"]["phase"] == "VERIFIED"
    assert data["run"]["provider_mutation_requests"] == 1
    assert data["proof"]["verdict"] == "PASSED"
    assert data["proof"]["recovery_mutation_calls"] == 0
    assert data["proof"]["semantic_effect_count"] == 1
    assert data["proof"]["duplicate_effects"] == 0
    assert data["proof"]["version_delta"] == 1
    assert data["proof"]["supplier_notified"] is False


def test_repeated_recovery_is_idempotent() -> None:
    reset_demo()
    run_id = create_run()
    client.post(f"/api/runs/{run_id}/execute")

    first = client.post(f"/api/runs/{run_id}/recover")
    second = client.post(f"/api/runs/{run_id}/recover")

    assert first.status_code == 200
    assert second.status_code == 200
    proof = second.json()["proof"]
    assert proof["provider_mutation_requests_after_recovery"] == 1
    assert proof["provider_commits"] == 1
    assert proof["semantic_effect_count"] == 1
    assert proof["duplicate_effects"] == 0

def test_dashboard_is_served() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="boundary-dashboard"' in response.text
    assert "BOUNDARY" in response.text


def test_api_overview_remains_available_as_json() -> None:
    response = client.get("/api")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert data["project"] == "BOUNDARY Recovery Agent"
    assert data["dashboard"] == "/"
    assert data["docs"] == "/docs"
