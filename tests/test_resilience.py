from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient

from app.main import app, open_db, provider_commit, utc_now


client = TestClient(app)


def create_run(scenario: str) -> str:
    reset = client.post("/api/demo/reset")
    assert reset.status_code == 200
    created = client.post("/api/runs", json={"scenario": scenario})
    assert created.status_code == 201
    return created.json()["run"]["id"]


def create_incident(scenario: str) -> str:
    run_id = create_run(scenario)
    incident = client.post(f"/api/runs/{run_id}/execute")
    assert incident.status_code == 503
    return run_id


def test_concurrent_recovery_has_one_agent_and_one_effect() -> None:
    run_id = create_incident("no_commit")
    workers = 8
    barrier = threading.Barrier(workers)

    def recover() -> int:
        barrier.wait()
        with TestClient(app) as concurrent_client:
            return concurrent_client.post(
                f"/api/runs/{run_id}/recover"
            ).status_code

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(lambda _: recover(), range(workers)))

    assert all(status == 200 for status in statuses)
    final = client.get(f"/api/runs/{run_id}").json()
    assert final["run"]["phase"] == "VERIFIED"
    assert final["run"]["recovery_mutation_calls"] == 1
    assert final["run"]["provider_commits"] == 1
    assert len(final["provider"]["effects"]) == 1
    assert final["proof"]["duplicate_effects"] == 0
    assert len(final["agent_trace"]) == 1


def test_concurrent_execute_allows_one_dispatch() -> None:
    run_id = create_run("ack_lost")
    workers = 4
    barrier = threading.Barrier(workers)

    def execute() -> int:
        barrier.wait()
        with TestClient(app) as concurrent_client:
            return concurrent_client.post(
                f"/api/runs/{run_id}/execute"
            ).status_code

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(lambda _: execute(), range(workers)))

    assert statuses.count(503) == 1
    assert statuses.count(409) == workers - 1
    final = client.get(f"/api/runs/{run_id}").json()
    assert final["run"]["provider_commits"] == 1
    assert len(final["provider"]["effects"]) == 1


def simulate_process_stop_at_dispatch(run_id: str) -> None:
    with open_db() as connection:
        connection.execute(
            """
            UPDATE runs
            SET phase = 'EXECUTING', effect_status = 'DISPATCHED', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )


def test_restart_before_provider_commit_recovers_once() -> None:
    run_id = create_run("no_commit")
    simulate_process_stop_at_dispatch(run_id)

    recovered = client.post(f"/api/runs/{run_id}/recover")
    assert recovered.status_code == 200
    data = recovered.json()
    assert data["run"]["phase"] == "VERIFIED"
    assert data["latest_reasoning"]["diagnosis"]["top_hypothesis"] == (
        "NOT_COMMITTED_REQUEST_LOST"
    )
    assert data["run"]["recovery_mutation_calls"] == 1
    assert data["run"]["provider_commits"] == 1
    assert data["proof"]["semantic_effect_count"] == 1
    assert data["proof"]["duplicate_effects"] == 0


def test_restart_after_provider_commit_reconciles_without_replay() -> None:
    run_id = create_run("ack_lost")
    simulate_process_stop_at_dispatch(run_id)
    provider_commit(run_id)

    recovered = client.post(f"/api/runs/{run_id}/recover")
    assert recovered.status_code == 200
    data = recovered.json()
    assert data["run"]["phase"] == "VERIFIED"
    assert data["latest_reasoning"]["diagnosis"]["top_hypothesis"] == (
        "COMMITTED_ACK_LOST"
    )
    assert data["run"]["recovery_mutation_calls"] == 0
    assert data["run"]["provider_commits"] == 1
    assert data["proof"]["semantic_effect_count"] == 1
    assert data["proof"]["duplicate_effects"] == 0
