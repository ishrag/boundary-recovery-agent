from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = REPOSITORY_ROOT / "app" / "static" / "validation.json"
COMPLETED_EXIT_CODES = {
    int(pytest.ExitCode.OK),
    int(pytest.ExitCode.TESTS_FAILED),
    int(pytest.ExitCode.NO_TESTS_COLLECTED),
}
STAT_KEYS = (
    "passed",
    "failed",
    "skipped",
    "error",
    "xfailed",
    "xpassed",
    "deselected",
)


class ValidationResults:
    def __init__(self) -> None:
        self.collected = 0
        self.counts = {key: 0 for key in STAT_KEYS}
        self.session_finished = False

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = len(session.items)

    def pytest_sessionfinish(
        self,
        session: pytest.Session,
        exitstatus: pytest.ExitCode,
    ) -> None:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            self.counts = {
                key: len(reporter.stats.get(key, ())) for key in STAT_KEYS
            }
            self.session_finished = True


def git_metadata() -> dict[str, Any]:
    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={REPOSITORY_ROOT}",
                "-C",
                str(REPOSITORY_ROOT),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    revision_result = git("rev-parse", "HEAD")
    status_result = git("status", "--porcelain")
    revision = (
        revision_result.stdout.strip()
        if revision_result.returncode == 0
        else None
    )
    dirty = (
        bool(status_result.stdout.strip())
        if status_result.returncode == 0
        else None
    )
    return {"revision": revision, "dirty": dirty}


def outcome_for(exit_code: int) -> str:
    if exit_code == int(pytest.ExitCode.OK):
        return "passed"
    if exit_code == int(pytest.ExitCode.TESTS_FAILED):
        return "failed"
    return "no_tests_collected"


def write_atomically(payload: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, OUTPUT_PATH)


def main(arguments: list[str] | None = None) -> int:
    pytest_arguments = list(arguments if arguments is not None else sys.argv[1:])
    results = ValidationResults()
    metadata = git_metadata()

    previous_directory = Path.cwd()
    os.chdir(REPOSITORY_ROOT)
    try:
        exit_code = int(pytest.main(pytest_arguments, plugins=[results]))
    finally:
        os.chdir(previous_directory)

    if results.session_finished and exit_code in COMPLETED_EXIT_CODES:
        verified_at = datetime.now(timezone.utc).isoformat()
        outcome = outcome_for(exit_code)
        payload = {
            "schema_version": 1,
            "generated_at": verified_at,
            "completed": True,
            "status": outcome,
            "passed": results.counts["passed"],
            "failed": results.counts["failed"] + results.counts["error"],
            "skipped": results.counts["skipped"],
            "verified_at": verified_at,
            "revision": metadata["revision"] or "unavailable",
            "dirty": metadata["dirty"],
            "command": "pytest" + (
                " " + subprocess.list2cmdline(pytest_arguments)
                if pytest_arguments
                else ""
            ),
            "git": metadata,
            "pytest": {
                "version": pytest.__version__,
                "exit_code": exit_code,
                "outcome": outcome,
                "collected": results.collected,
                "counts": results.counts,
            },
        }
        write_atomically(payload)
        print(f"Validation artifact written to {OUTPUT_PATH}")
    else:
        print(
            "Pytest did not complete normally; validation.json was not updated.",
            file=sys.stderr,
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
