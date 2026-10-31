from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from starlette.testclient import TestClient
import pytest

from provider_service.main import app as provider_app


def provider_payload() -> dict:
    return {
        "run_id": "contract01",
        "operation_id": "OP-CONTRACT01",
        "idempotency_key": "boundary:contract01:reschedule",
        "payload_hash": "a" * 64,
        "order_id": "731",
        "scheduled_for": "Friday 10:00",
        "notify_supplier": False,
    }


@pytest.fixture
def provider_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv(
        "BOUNDARY_PROVIDER_DB_PATH", str(tmp_path / "provider-contract.db")
    )
    client = TestClient(provider_app)
    response = client.post("/v1/internal/reset")
    assert response.status_code == 200
    return client


def test_concurrent_same_key_commits_exactly_once(
    provider_client: TestClient,
) -> None:
    workers = 6
    barrier = threading.Barrier(workers)

    def commit() -> tuple[int, dict]:
        barrier.wait()
        with TestClient(provider_app) as concurrent_client:
            response = concurrent_client.post(
                "/v1/operations", json=provider_payload()
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: commit(), range(workers)))

    statuses = [status for status, _ in results]
    receipts = [body["receipt"] for _, body in results]
    assert statuses.count(201) == 1
    assert statuses.count(200) == workers - 1
    assert all(receipt == receipts[0] for receipt in receipts)

    effects = provider_client.get("/v1/effects/contract01")
    order = provider_client.get("/v1/orders/731")
    assert effects.status_code == 200
    assert len(effects.json()) == 1
    assert order.json()["version"] == 13


def test_same_key_with_different_work_is_rejected(
    provider_client: TestClient,
) -> None:
    accepted = provider_client.post("/v1/operations", json=provider_payload())
    assert accepted.status_code == 201

    conflicting = provider_payload()
    conflicting["payload_hash"] = "b" * 64
    rejected = provider_client.post("/v1/operations", json=conflicting)
    assert rejected.status_code == 409
    assert len(provider_client.get("/v1/effects/contract01").json()) == 1


def test_outage_is_unknown_not_authoritative_absence(
    provider_client: TestClient,
) -> None:
    key = provider_payload()["idempotency_key"]
    fault = provider_client.post(
        f"/v1/internal/faults/{key}",
        json={"lookup_failures_remaining": -1},
    )
    assert fault.status_code == 200

    lookup = provider_client.get(f"/v1/receipts/{key}")
    assert lookup.status_code == 503
    assert lookup.status_code != 404
