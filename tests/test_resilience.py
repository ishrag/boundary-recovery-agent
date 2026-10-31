from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import textwrap

from starlette.testclient import TestClient
import pytest

from app.main import app, open_db, provider_commit, utc_now


client = TestClient(app)
REPO_ROOT = Path(__file__).resolve().parents[1]


CRASH_WORKER = textwrap.dedent(
    r'''
    import json
    import os
    import sys
    from pathlib import Path

    from starlette.testclient import TestClient

    from app.main import (
        PROCESS_INSTANCE_ID,
        app,
        open_db,
        provider_commit,
        utc_now,
    )

    state_path = Path(sys.argv[1])
    commit_before_crash = sys.argv[2] == "1"

    with TestClient(app) as client:
        reset = client.post("/api/demo/reset")
        assert reset.status_code == 200, reset.text
        created = client.post("/api/runs", json={"scenario": "ack_lost"})
        assert created.status_code == 201, created.text
        run_id = created.json()["run"]["id"]

    with open_db() as connection:
        cursor = connection.execute(
            """
            UPDATE runs
            SET phase = 'EXECUTING',
                effect_status = 'DISPATCHED',
                updated_at = ?
            WHERE id = ? AND phase = 'READY'
            """,
            (utc_now(), run_id),
        )
        assert cursor.rowcount == 1

    state_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "crash_process_instance": PROCESS_INSTANCE_ID,
                "crash_pid": os.getpid(),
                "commit_before_crash": commit_before_crash,
            }
        ),
        encoding="utf-8",
    )

    if commit_before_crash:
        provider_commit(run_id)

    os._exit(91)
    '''
)


RECOVERY_WORKER = textwrap.dedent(
    r"""
    import json
    import os
    import sys
    from pathlib import Path

    from starlette.testclient import TestClient

    from app.main import PROCESS_INSTANCE_ID, app, open_db

    state_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    run_id = json.loads(state_path.read_text(encoding="utf-8"))["run_id"]

    with TestClient(app) as client:
        response = client.post(f"/api/runs/{run_id}/recover")
        body = response.json()

    with open_db() as connection:
        lease_count = connection.execute(
            "SELECT COUNT(*) FROM recovery_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]

    result_path.write_text(
        json.dumps(
            {
                "status_code": response.status_code,
                "body": body,
                "recovery_process_instance": PROCESS_INSTANCE_ID,
                "recovery_pid": os.getpid(),
                "lease_count": lease_count,
            }
        ),
        encoding="utf-8",
    )
    """
)


def run_isolated_worker(
    code: str,
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *arguments],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


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


@pytest.mark.parametrize(
    (
        "commit_before_crash",
        "expected_diagnosis",
        "expected_tool",
        "expected_recovery_mutations",
    ),
    [
        pytest.param(
            False,
            "NOT_COMMITTED_REQUEST_LOST",
            "retry_same_key",
            1,
            id="crash-before-provider-commit",
        ),
        pytest.param(
            True,
            "COMMITTED_ACK_LOST",
            "reconcile_receipt",
            0,
            id="crash-after-provider-commit",
        ),
    ],
)
def test_real_process_crash_is_recovered_by_fresh_process(
    tmp_path: Path,
    commit_before_crash: bool,
    expected_diagnosis: str,
    expected_tool: str,
    expected_recovery_mutations: int,
) -> None:
    database_path = tmp_path / "boundary-crash.db"
    provider_database_path = tmp_path / "provider-crash.db"
    state_path = tmp_path / "crash-state.json"
    result_path = tmp_path / "recovery-result.json"

    environment = os.environ.copy()
    environment["BOUNDARY_DB_PATH"] = str(database_path)
    environment["BOUNDARY_PROVIDER_DB_PATH"] = str(provider_database_path)
    environment.pop("BOUNDARY_PROVIDER_URL", None)
    existing_python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not existing_python_path
        else str(REPO_ROOT) + os.pathsep + existing_python_path
    )

    crashed = run_isolated_worker(
        CRASH_WORKER,
        str(state_path),
        "1" if commit_before_crash else "0",
        environment=environment,
    )
    assert crashed.returncode == 91, (
        "Crash worker did not stop at the intended boundary.\n"
        f"stdout:\n{crashed.stdout}\nstderr:\n{crashed.stderr}"
    )
    crash_state = json.loads(state_path.read_text(encoding="utf-8"))

    restarted = run_isolated_worker(
        RECOVERY_WORKER,
        str(state_path),
        str(result_path),
        environment=environment,
    )
    assert restarted.returncode == 0, (
        "Fresh recovery process failed.\n"
        f"stdout:\n{restarted.stdout}\nstderr:\n{restarted.stderr}"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    data = result["body"]

    assert result["recovery_pid"] != crash_state["crash_pid"]
    assert result["recovery_process_instance"] != (
        crash_state["crash_process_instance"]
    )
    assert result["status_code"] == 200
    assert result["lease_count"] == 0
    assert data["run"]["phase"] == "VERIFIED"
    assert data["run"]["provider_commits"] == 1
    assert data["run"]["recovery_mutation_calls"] == (
        expected_recovery_mutations
    )
    assert len(data["provider"]["effects"]) == 1
    assert len(data["agent_trace"]) == 1

    latest = data["latest_reasoning"]
    assert latest["diagnosis"]["top_hypothesis"] == expected_diagnosis
    assert latest["tool_name"] == expected_tool
    assert latest["verification"]["passed"] is True
    assert data["proof"]["verdict"] == "PASSED"
    assert data["proof"]["semantic_effect_count"] == 1
    assert data["proof"]["duplicate_effects"] == 0
    assert data["proof"]["version_delta"] == 1

    with sqlite3.connect(database_path) as boundary_database:
        boundary_tables = {
            row[0]
            for row in boundary_database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    with sqlite3.connect(provider_database_path) as provider_database:
        provider_tables = {
            row[0]
            for row in provider_database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert not {
        "provider_orders",
        "provider_operations",
        "provider_effects",
    } & boundary_tables
    assert {
        "provider_orders",
        "provider_operations",
        "provider_effects",
    } <= provider_tables

    event_types = [event["event_type"] for event in data["timeline"]]
    assert "PROCESS_RESTART_DETECTED" in event_types
    assert "RECOVERY_VERIFIED" in event_types
