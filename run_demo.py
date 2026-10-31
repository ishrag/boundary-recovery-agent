from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


REPOSITORY_ROOT = Path(__file__).resolve().parent
PROVIDER_URL = "http://127.0.0.1:8001"
BOUNDARY_URL = "http://127.0.0.1:8000"


def wait_until_healthy(
    url: str, process: subprocess.Popen[bytes], timeout: float = 15
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service exited before becoming healthy: {url}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"Service did not become healthy: {url}")


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    provider_environment = os.environ.copy()
    provider_environment.pop("BOUNDARY_PROVIDER_URL", None)
    provider_environment.setdefault(
        "BOUNDARY_PROVIDER_DB_PATH",
        str(REPOSITORY_ROOT / "provider.db"),
    )
    provider = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "provider_service.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
        ],
        cwd=REPOSITORY_ROOT,
        env=provider_environment,
    )
    boundary: subprocess.Popen[bytes] | None = None
    try:
        wait_until_healthy(PROVIDER_URL + "/health", provider)
        boundary_environment = os.environ.copy()
        boundary_environment["BOUNDARY_PROVIDER_URL"] = PROVIDER_URL
        boundary_environment.setdefault(
            "BOUNDARY_DB_PATH", str(REPOSITORY_ROOT / "boundary.db")
        )
        boundary = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=REPOSITORY_ROOT,
            env=boundary_environment,
        )
        wait_until_healthy(BOUNDARY_URL + "/ready", boundary)
        print("BOUNDARY is ready at http://127.0.0.1:8000")
        print("Provider health is at http://127.0.0.1:8001/health")
        print("Stop this PyCharm run to stop both services.")
        return boundary.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if boundary is not None:
            stop_process(boundary)
        stop_process(provider)


if __name__ == "__main__":
    raise SystemExit(main())

