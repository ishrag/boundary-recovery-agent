from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app.main as boundary


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setattr(boundary, "DB_PATH", tmp_path / "boundary-inputs.db")
    monkeypatch.setenv(
        "BOUNDARY_PROVIDER_DB_PATH", str(tmp_path / "provider-inputs.db")
    )
    monkeypatch.delenv("BOUNDARY_PROVIDER_URL", raising=False)
    boundary.init_db()
    test_client = TestClient(boundary.app)
    reset = test_client.post("/api/demo/reset")
    assert reset.status_code == 200
    return test_client


def test_custom_schedule_and_supplier_notification_are_recovered(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/runs",
        json={
            "scenario": "ack_lost",
            "order_id": "731",
            "scheduled_for": "  Saturday 14:30  ",
            "notify_supplier": True,
        },
    )
    assert created.status_code == 201
    run_id = created.json()["run"]["id"]
    assert created.json()["run"]["scheduled_for"] == "Saturday 14:30"
    assert created.json()["run"]["notify_supplier"] is True

    incident = client.post(f"/api/runs/{run_id}/execute")
    assert incident.status_code == 503
    recovered = client.post(f"/api/runs/{run_id}/recover")
    assert recovered.status_code == 200

    data = recovered.json()
    assert data["run"]["phase"] == "VERIFIED"
    assert data["provider"]["order"]["scheduled_for"] == "Saturday 14:30"
    assert data["provider"]["order"]["supplier_notified"] is True
    assert data["proof"]["supplier_notified"] is True
    assert data["proof"]["verdict"] == "PASSED"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"order_id": "   "}, id="blank-order-id"),
        pytest.param({"scheduled_for": "\t  "}, id="blank-schedule"),
        pytest.param({"order_id": "x" * 129}, id="oversized-order-id"),
        pytest.param(
            {"scheduled_for": "x" * 201}, id="oversized-schedule"
        ),
    ],
)
def test_blank_and_oversized_text_inputs_are_rejected(
    client: TestClient, payload: dict[str, str]
) -> None:
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 422


def test_unknown_order_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={"order_id": "missing-order", "scheduled_for": "Friday 10:00"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
