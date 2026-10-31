import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

from starlette.testclient import TestClient
import pytest

from app.main import app


REPO_ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def external_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    port = free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    provider_database = tmp_path / "independent-provider.db"
    environment = os.environ.copy()
    environment["BOUNDARY_PROVIDER_DB_PATH"] = str(provider_database)
    environment.pop("BOUNDARY_PROVIDER_URL", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "provider_service.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("The independent provider process exited early")
            try:
                with urllib.request.urlopen(
                    base_url + "/health", timeout=0.5
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("The independent provider did not become healthy")

        monkeypatch.setenv("BOUNDARY_PROVIDER_URL", base_url)
        yield provider_database
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_full_recovery_crosses_a_real_http_process_boundary(
    external_provider: Path,
) -> None:
    reset = client.post("/api/demo/reset")
    assert reset.status_code == 200

    created = client.post("/api/runs", json={"scenario": "ack_lost"})
    assert created.status_code == 201
    run_id = created.json()["run"]["id"]

    incident = client.post(f"/api/runs/{run_id}/execute")
    assert incident.status_code == 503
    recovered = client.post(f"/api/runs/{run_id}/recover")
    assert recovered.status_code == 200
    data = recovered.json()

    assert external_provider.exists()
    assert data["provider"]["mode"] == "http"
    assert data["run"]["phase"] == "VERIFIED"
    assert data["run"]["provider_commits"] == 1
    assert len(data["provider"]["effects"]) == 1
    assert data["proof"]["verdict"] == "PASSED"
    assert data["proof"]["provider_available_during_proof"] is True
    assert data["proof"]["duplicate_effects"] == 0
