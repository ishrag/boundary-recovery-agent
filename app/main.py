from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.diagnosis import diagnose, model_card
from app.provider_gateway import (
    ProviderConflict,
    ProviderNotFound,
    ProviderUnavailable,
    commit_provider_operation,
    configure_provider_fault,
    get_provider_order as fetch_provider_order,
    list_provider_effects,
    lookup_provider_receipt,
    provider_health,
    provider_mode,
    reset_provider,
)
from provider_service.store import PROVIDER_SOURCE


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(
    os.getenv("BOUNDARY_DB_PATH", str(APP_DIR.parent / "boundary.db"))
)
DASHBOARD_PATH = APP_DIR / "static" / "index.html"
PROCESS_INSTANCE_ID = uuid.uuid4().hex
RECOVERY_LEASE_SECONDS = 30
SUPPORTED_SCENARIOS = {
    "ack_lost",
    "no_commit",
    "provider_unavailable",
    "receipt_conflict",
    "compound",
}
MIN_DIAGNOSIS_CONFIDENCE = 0.75
MIN_DIAGNOSIS_MARGIN = 0.20
MAX_AGENT_STEPS = 3


app = FastAPI(
    title="BOUNDARY Recovery Agent",
    description=(
        "An evidence-driven recovery agent that diagnoses uncertain external "
        "actions, selects a safe strategy, acts through allow-listed tools, "
        "and verifies the result."
    ),
    version="0.4.0",
)


class CreateRunRequest(BaseModel):
    scenario: str = "ack_lost"
    order_id: str = "731"
    scheduled_for: str = "Friday 10:00"
    notify_supplier: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def open_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with open_db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs
            (
                id TEXT PRIMARY KEY,
                scenario TEXT NOT NULL,
                phase TEXT NOT NULL,
                connection TEXT NOT NULL,
                order_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                notify_supplier INTEGER NOT NULL,
                operation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                effect_status TEXT NOT NULL,
                receipt_json TEXT,
                provider_mutation_requests INTEGER NOT NULL DEFAULT 0,
                provider_commits INTEGER NOT NULL DEFAULT 0,
                recovery_lookup_calls INTEGER NOT NULL DEFAULT 0,
                recovery_mutation_calls INTEGER NOT NULL DEFAULT 0,
                order_version_before INTEGER NOT NULL,
                order_version_after INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_steps
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                step_no INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                diagnosis_json TEXT NOT NULL,
                proposed_strategy TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_result_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, step_no)
            );

            CREATE TABLE IF NOT EXISTS recovery_leases
            (
                run_id TEXT PRIMARY KEY,
                owner_instance TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


init_db()


def get_run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return row


def add_event(
    connection: sqlite3.Connection,
    run_id: str,
    event_type: str,
    detail: str,
) -> None:
    connection.execute(
        """
        INSERT INTO events (run_id, event_type, detail, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, event_type, detail, utc_now()),
    )


def serialize_run(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["notify_supplier"] = bool(result["notify_supplier"])
    result["payload"] = json.loads(result.pop("payload_json"))
    receipt_json = result.pop("receipt_json")
    result["receipt"] = json.loads(receipt_json) if receipt_json else None
    return result


def provider_order(
    connection: sqlite3.Connection, order_id: str
) -> dict[str, Any]:
    del connection
    try:
        return fetch_provider_order(order_id)
    except ProviderNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailable as error:
        raise HTTPException(
            status_code=503, detail="Provider order service is unavailable"
        ) from error


def provider_commit(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        connection.execute(
            """
            UPDATE runs
            SET provider_mutation_requests = provider_mutation_requests + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )
        provider_request = {
            "run_id": run_id,
            "operation_id": run["operation_id"],
            "idempotency_key": run["idempotency_key"],
            "payload_hash": run["payload_hash"],
            "order_id": run["order_id"],
            "scheduled_for": run["scheduled_for"],
            "notify_supplier": bool(run["notify_supplier"]),
        }

    try:
        result = commit_provider_operation(provider_request)
    except ProviderConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProviderNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailable as error:
        raise HTTPException(
            status_code=503, detail="Provider mutation outcome is unknown"
        ) from error

    receipt = result["receipt"]
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        connection.execute(
            """
            UPDATE runs
            SET provider_commits = CASE
                    WHEN provider_commits < 1 THEN 1
                    ELSE provider_commits
                END,
                order_version_after = ?, updated_at = ?
            WHERE id = ?
            """,
            (receipt["order_version"], utc_now(), run_id),
        )
        if result["created"]:
            add_event(
                connection,
                run_id,
                "PROVIDER_COMMITTED",
                (
                    f"Independent provider changed order {run['order_id']} to "
                    f"{run['scheduled_for']} at version "
                    f"{receipt['order_version']}."
                ),
            )
    return receipt


def expected_receipt(run: sqlite3.Row) -> dict[str, Any]:
    return {
        "outcome": "committed",
        "provider_source": PROVIDER_SOURCE,
        "operation_id": run["operation_id"],
        "idempotency_key": run["idempotency_key"],
        "payload_hash": run["payload_hash"],
        "order_id": run["order_id"],
        "scheduled_for": run["scheduled_for"],
        "supplier_notified": bool(run["notify_supplier"]),
    }


def receipt_mismatches(
    run: sqlite3.Row, receipt: dict[str, Any]
) -> list[str]:
    return [
        field
        for field, expected_value in expected_receipt(run).items()
        if receipt.get(field) != expected_value
    ]


def count_bucket(value: int) -> str:
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    return "many"


def delta_bucket(value: int) -> str:
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    return "many"


def transport_signal(effect_status: str) -> str:
    mapping = {
        "ACK_LOST": "ack_lost",
        "REQUEST_LOST": "request_lost",
        "PROVIDER_TIMEOUT": "timeout",
        "OUTCOME_UNKNOWN": "timeout",
    }
    return mapping.get(effect_status, "none")


def observe_incident(
    run_id: str, step_no: int
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        connection.execute(
            """
            UPDATE runs
            SET phase = 'DIAGNOSING',
                connection = 'RECONNECTING',
                recovery_lookup_calls = recovery_lookup_calls + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )

    base: dict[str, Any] = {
        "request_persisted": "yes",
        "local_receipt_present": "yes" if run["receipt_json"] else "no",
        "provider_reachable": "unknown",
        "provider_operation": "unknown",
        "receipt_integrity": "unknown",
        "effect_count": "unknown",
        "version_delta": "unknown",
        "state_matches_intent": "unknown",
        "transport_signal": transport_signal(run["effect_status"]),
        "lookup_error": "none",
        "effect_ids": [],
        "provider_mutation_requests": run["provider_mutation_requests"],
        "provider_commits": run["provider_commits"],
        "recovery_mutation_calls": run["recovery_mutation_calls"],
    }

    try:
        receipt = lookup_provider_receipt(run["idempotency_key"])
        order = fetch_provider_order(run["order_id"])
        effects = list_provider_effects(run_id)
    except (ProviderUnavailable, ProviderNotFound):
        base.update(
            {
                "provider_reachable": "no",
                "lookup_error": "unavailable",
            }
        )
        with open_db() as connection:
            connection.execute(
                """
                UPDATE runs
                SET connection = 'UNAVAILABLE', updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), run_id),
            )
            add_event(
                connection,
                run_id,
                "AGENT_OBSERVED",
                (
                    f"Step {step_no}: the independent provider could not "
                    "return authoritative evidence."
                ),
            )
        return base, None

    effect_ids = [effect["effect_id"] for effect in effects]
    version_delta = order["version"] - run["order_version_before"]
    state_matches = (
        order["scheduled_for"] == run["scheduled_for"]
        and bool(order["supplier_notified"])
        == bool(run["notify_supplier"])
    )
    if receipt is not None:
        mismatches = receipt_mismatches(run, receipt)
        integrity = "mismatch" if mismatches else "match"
    else:
        integrity = "unknown"

    authoritative_commits = 1 if effect_ids else run["provider_commits"]
    with open_db() as connection:
        connection.execute(
            """
            UPDATE runs
            SET connection = 'RECONNECTING',
                provider_commits = CASE
                    WHEN provider_commits < ? THEN ? ELSE provider_commits
                END,
                order_version_after = CASE
                    WHEN ? > 0 THEN ? ELSE order_version_after
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                authoritative_commits,
                authoritative_commits,
                len(effect_ids),
                order["version"],
                utc_now(),
                run_id,
            ),
        )
        add_event(
            connection,
            run_id,
            "AGENT_OBSERVED",
            (
                f"Step {step_no}: collected evidence across the independent "
                "provider service boundary."
            ),
        )

    base.update(
        {
            "provider_reachable": "yes",
            "provider_operation": "present" if receipt else "absent",
            "receipt_integrity": integrity,
            "effect_count": count_bucket(len(effect_ids)),
            "version_delta": delta_bucket(version_delta),
            "state_matches_intent": "yes" if state_matches else "no",
            "lookup_error": (
                "integrity_conflict" if integrity == "mismatch" else "none"
            ),
            "effect_ids": effect_ids,
            "provider_order_version": order["version"],
            "provider_commits": authoritative_commits,
        }
    )
    return base, receipt


def strategy_for(diagnosis: dict[str, Any]) -> dict[str, str]:
    mapping = {
        "COMMITTED_ACK_LOST": {
            "code": "RECONCILE_RECEIPT",
            "tool": "reconcile_receipt",
            "reason": "Matching authoritative evidence proves the effect exists.",
        },
        "NOT_COMMITTED_REQUEST_LOST": {
            "code": "RETRY_SAME_KEY",
            "tool": "retry_same_key",
            "reason": "Authoritative absence makes one idempotent retry safe.",
        },
        "PROVIDER_UNAVAILABLE": {
            "code": "WAIT_AND_REOBSERVE",
            "tool": "wait_and_reobserve",
            "reason": "Unknown external state forbids a blind mutation.",
        },
        "RECEIPT_CONFLICT_OR_TAMPERED": {
            "code": "QUARANTINE_AND_ESCALATE",
            "tool": "quarantine_and_escalate",
            "reason": "Conflicting evidence must be preserved for human review.",
        },
        "DUPLICATE_OR_PARTIAL_EFFECT": {
            "code": "FREEZE_AND_ESCALATE",
            "tool": "freeze_and_escalate",
            "reason": "An inconsistent effect count makes automation unsafe.",
        },
    }
    return mapping[diagnosis["top_hypothesis"]]


def evaluate_policy(
    run: sqlite3.Row,
    evidence: dict[str, Any],
    diagnosis: dict[str, Any],
    strategy: dict[str, str],
) -> dict[str, Any]:
    if (
        diagnosis["confidence"] < MIN_DIAGNOSIS_CONFIDENCE
        or diagnosis["margin"] < MIN_DIAGNOSIS_MARGIN
    ):
        return {
            "authorized": False,
            "mutation_allowed": False,
            "decision": "FAIL_CLOSED",
            "reason": "The diagnosis is ambiguous; human review is required.",
        }

    tool = strategy["tool"]
    if tool == "reconcile_receipt":
        safe = all(
            [
                evidence["provider_reachable"] == "yes",
                evidence["provider_operation"] == "present",
                evidence["receipt_integrity"] == "match",
                evidence["effect_count"] == "one",
                evidence["version_delta"] == "one",
                evidence["state_matches_intent"] == "yes",
            ]
        )
        return {
            "authorized": safe,
            "mutation_allowed": False,
            "decision": "LOOKUP_ONLY" if safe else "FAIL_CLOSED",
            "reason": (
                "A matching receipt and one effect allow read-only reconciliation."
                if safe
                else "The receipt is not sufficiently bound to the original intent."
            ),
        }

    if tool == "retry_same_key":
        safe = all(
            [
                evidence["provider_reachable"] == "yes",
                evidence["provider_operation"] == "absent",
                evidence["effect_count"] == "zero",
                evidence["version_delta"] == "zero",
                evidence["state_matches_intent"] == "no",
                run["recovery_mutation_calls"] == 0,
            ]
        )
        return {
            "authorized": safe,
            "mutation_allowed": safe,
            "decision": "SAME_KEY_RETRY" if safe else "FAIL_CLOSED",
            "reason": (
                "The provider proves no effect exists; one retry with the "
                "original key is permitted."
                if safe
                else "Authoritative absence was not proven, so retry is blocked."
            ),
        }

    if tool == "wait_and_reobserve":
        return {
            "authorized": True,
            "mutation_allowed": False,
            "decision": "WAIT_ONLY",
            "reason": "Only another read-only observation is permitted.",
        }

    return {
        "authorized": True,
        "mutation_allowed": False,
        "decision": "ESCALATE_ONLY",
        "reason": "Automation is frozen and all external mutations are blocked.",
    }


def record_agent_step(
    run_id: str,
    step_no: int,
    evidence: dict[str, Any],
    diagnosis: dict[str, Any],
    strategy: dict[str, str],
    policy: dict[str, Any],
    tool_result: dict[str, Any],
    verification: dict[str, Any],
) -> None:
    with open_db() as connection:
        connection.execute(
            """
            INSERT INTO agent_steps
                (run_id, step_no, evidence_json, diagnosis_json,
                 proposed_strategy, policy_json, tool_name, tool_result_json,
                 verification_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step_no,
                canonical_json(evidence),
                canonical_json(diagnosis),
                strategy["code"],
                canonical_json(policy),
                strategy["tool"],
                canonical_json(tool_result),
                canonical_json(verification),
                utc_now(),
            ),
        )


def load_agent_trace(run_id: str) -> list[dict[str, Any]]:
    with open_db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_no
            """,
            (run_id,),
        ).fetchall()
    return [
        {
            "step_no": row["step_no"],
            "evidence": json.loads(row["evidence_json"]),
            "diagnosis": json.loads(row["diagnosis_json"]),
            "strategy": row["proposed_strategy"],
            "policy": json.loads(row["policy_json"]),
            "tool_name": row["tool_name"],
            "tool_result": json.loads(row["tool_result_json"]),
            "verification": json.loads(row["verification_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def block_run(run_id: str, effect_status: str, event: str, detail: str) -> None:
    with open_db() as connection:
        connection.execute(
            """
            UPDATE runs
            SET phase = 'BLOCKED', connection = 'CONNECTED',
                effect_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (effect_status, utc_now(), run_id),
        )
        add_event(connection, run_id, event, detail)


def verify_receipt(
    run_id: str, receipt: dict[str, Any] | None
) -> dict[str, Any]:
    if receipt is None:
        block_run(
            run_id,
            "NO_RECEIPT",
            "VERIFICATION_BLOCKED",
            "No authoritative receipt was available.",
        )
        return {"passed": False, "reason": "receipt_missing"}

    with open_db() as connection:
        run = get_run_row(connection, run_id)
    try:
        order = fetch_provider_order(run["order_id"])
        effects = list_provider_effects(run_id)
    except (ProviderUnavailable, ProviderNotFound):
        block_run(
            run_id,
            "VERIFICATION_UNAVAILABLE",
            "VERIFICATION_BLOCKED",
            "The provider became unavailable during verification.",
        )
        return {"passed": False, "reason": "provider_unavailable"}

    mismatches = receipt_mismatches(run, receipt)
    effect_ids = {effect["effect_id"] for effect in effects}
    if order["version"] != receipt.get("order_version"):
        mismatches.append("order_version")
    if order["scheduled_for"] != run["scheduled_for"]:
        mismatches.append("provider_state")
    if bool(order["supplier_notified"]) != bool(run["notify_supplier"]):
        mismatches.append("supplier_notification")
    if receipt.get("effect_id") not in effect_ids:
        mismatches.append("effect_id")

    with open_db() as connection:
        if mismatches:
            connection.execute(
                """
                UPDATE runs
                SET phase = 'BLOCKED', connection = 'CONNECTED',
                    effect_status = 'RECEIPT_REJECTED', updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), run_id),
            )
            add_event(
                connection,
                run_id,
                "VERIFICATION_REJECTED",
                "Evidence mismatch in: " + ", ".join(sorted(set(mismatches))),
            )
            return {"passed": False, "mismatches": sorted(set(mismatches))}

        connection.execute(
            """
            UPDATE runs
            SET phase = 'VERIFIED', connection = 'CONNECTED',
                effect_status = 'VERIFIED', receipt_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (canonical_json(receipt), utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "RECOVERY_VERIFIED",
            "Receipt, payload, effect and provider state all match.",
        )
        return {
            "passed": True,
            "receipt_payload_hash_matches": True,
            "provider_state_matches": True,
            "effect_id": receipt["effect_id"],
        }


def acquire_recovery_lease(run_id: str) -> tuple[str | None, int]:
    """Claim one recovery worker without holding a lock during tool calls."""
    owner_token = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=RECOVERY_LEASE_SECONDS)
    with open_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = get_run_row(connection, run_id)
        previous_steps = connection.execute(
            "SELECT COUNT(*) AS count FROM agent_steps WHERE run_id = ?",
            (run_id,),
        ).fetchone()["count"]

        if run["phase"] == "VERIFIED":
            return None, previous_steps
        if run["phase"] == "BLOCKED" and previous_steps:
            return None, previous_steps
        if run["phase"] not in {
            "INCIDENT",
            "BLOCKED",
            "EXECUTING",
            "RECOVERING",
            "DIAGNOSING",
        }:
            raise HTTPException(
                status_code=409, detail="This run has no recoverable incident."
            )

        existing = connection.execute(
            "SELECT * FROM recovery_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            lease_expired = datetime.fromisoformat(existing["expires_at"]) <= now
            if not lease_expired:
                return None, previous_steps
            connection.execute(
                "DELETE FROM recovery_leases WHERE run_id = ?", (run_id,)
            )
            add_event(
                connection,
                run_id,
                "RECOVERY_LEASE_RECLAIMED",
                "A crashed or stalled recovery worker was safely replaced.",
            )

        if run["phase"] == "EXECUTING":
            connection.execute(
                """
                UPDATE runs
                SET phase = 'INCIDENT', connection = 'LOST',
                    effect_status = 'OUTCOME_UNKNOWN', updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), run_id),
            )
            add_event(
                connection,
                run_id,
                "PROCESS_RESTART_DETECTED",
                (
                    "The process stopped at the provider boundary. "
                    "Recovery will inspect authoritative evidence before acting."
                ),
            )

        connection.execute(
            """
            INSERT INTO recovery_leases
                (run_id, owner_instance, owner_token, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                PROCESS_INSTANCE_ID,
                owner_token,
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE runs
            SET phase = 'RECOVERING', connection = 'RECONNECTING', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )
    return owner_token, previous_steps


def release_recovery_lease(run_id: str, owner_token: str) -> None:
    with open_db() as connection:
        connection.execute(
            """
            DELETE FROM recovery_leases
            WHERE run_id = ? AND owner_token = ?
            """,
            (run_id, owner_token),
        )


def run_recovery_agent(run_id: str) -> None:
    owner_token, previous_steps = acquire_recovery_lease(run_id)
    if owner_token is None:
        return

    try:
        for step_no in range(previous_steps + 1, MAX_AGENT_STEPS + 1):
            evidence, receipt = observe_incident(run_id, step_no)
            diagnosis = diagnose(evidence)
            strategy = strategy_for(diagnosis)
            with open_db() as connection:
                run = get_run_row(connection, run_id)
            policy = evaluate_policy(run, evidence, diagnosis, strategy)

            if not policy["authorized"]:
                block_run(
                    run_id,
                    "AMBIGUOUS_DIAGNOSIS",
                    "AGENT_ESCALATED",
                    "Diagnosis did not meet the confidence and safety thresholds.",
                )
                record_agent_step(
                    run_id,
                    step_no,
                    evidence,
                    diagnosis,
                    strategy,
                    policy,
                    {"status": "blocked"},
                    {"passed": False, "reason": "policy_denied"},
                )
                return

            tool = strategy["tool"]
            if tool == "wait_and_reobserve":
                final = step_no == MAX_AGENT_STEPS
                if final:
                    block_run(
                        run_id,
                        "PROVIDER_UNAVAILABLE",
                        "AGENT_ESCALATED",
                        "Provider remained unavailable; no mutation was attempted.",
                    )
                else:
                    with open_db() as connection:
                        connection.execute(
                            """
                            UPDATE runs
                            SET phase = 'INCIDENT', connection = 'LOST', updated_at = ?
                            WHERE id = ?
                            """,
                            (utc_now(), run_id),
                        )
                        add_event(
                            connection,
                            run_id,
                            "AGENT_WAITED",
                            (
                                "The agent waited safely and scheduled another "
                                "observation."
                            ),
                        )
                record_agent_step(
                    run_id,
                    step_no,
                    evidence,
                    diagnosis,
                    strategy,
                    policy,
                    {"status": "escalated" if final else "reobserve"},
                    {
                        "passed": final,
                        "safe": True,
                        "mutation_attempted": False,
                    },
                )
                if final:
                    return
                continue

            if tool == "reconcile_receipt":
                verification = verify_receipt(run_id, receipt)
                record_agent_step(
                    run_id,
                    step_no,
                    evidence,
                    diagnosis,
                    strategy,
                    policy,
                    {"status": "receipt_reconciled", "mutation_sent": False},
                    verification,
                )
                return

            if tool == "retry_same_key":
                with open_db() as connection:
                    connection.execute(
                        """
                        UPDATE runs
                        SET recovery_mutation_calls = recovery_mutation_calls + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (utc_now(), run_id),
                    )
                    add_event(
                        connection,
                        run_id,
                        "SAFE_RETRY_AUTHORIZED",
                        (
                            "Authoritative absence allowed one retry with the "
                            "original key."
                        ),
                    )
                retry_receipt = provider_commit(run_id)
                verification = verify_receipt(run_id, retry_receipt)
                record_agent_step(
                    run_id,
                    step_no,
                    evidence,
                    diagnosis,
                    strategy,
                    policy,
                    {
                        "status": "same_key_retry_completed",
                        "idempotency_key_reused": True,
                    },
                    verification,
                )
                return

            status = (
                "RECEIPT_QUARANTINED"
                if tool == "quarantine_and_escalate"
                else "EFFECTS_FROZEN"
            )
            block_run(
                run_id,
                status,
                "AGENT_ESCALATED",
                strategy["reason"],
            )
            record_agent_step(
                run_id,
                step_no,
                evidence,
                diagnosis,
                strategy,
                policy,
                {"status": "escalated", "mutation_sent": False},
                {"passed": True, "safe": True, "requires_human": True},
            )
            return
    finally:
        release_recovery_lease(run_id, owner_token)


def build_proof(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
    provider_available = True
    try:
        order = fetch_provider_order(run["order_id"])
        effects = list_provider_effects(run_id)
    except (ProviderUnavailable, ProviderNotFound):
        provider_available = False
        order = {
            "order_id": run["order_id"],
            "scheduled_for": run["scheduled_for"],
            "supplier_notified": bool(run["notify_supplier"]),
            "version": run["order_version_after"]
            or run["order_version_before"],
        }
        effects = []
    trace = load_agent_trace(run_id)
    effect_ids = [row["effect_id"] for row in effects]
    duplicate_effects = max(0, len(effect_ids) - 1)
    version_delta = order["version"] - run["order_version_before"]
    receipt = json.loads(run["receipt_json"]) if run["receipt_json"] else None
    used_safe_retry = run["recovery_mutation_calls"] > 0
    expected_recovery_mutations = 1 if used_safe_retry else 0
    first_evidence = trace[0]["evidence"] if trace else {}
    final_step = trace[-1] if trace else {}

    passed = all(
        [
            provider_available,
            run["phase"] == "VERIFIED",
            run["provider_mutation_requests"] == 1,
            run["provider_commits"] == 1,
            run["recovery_lookup_calls"] >= 1,
            run["recovery_mutation_calls"] == expected_recovery_mutations,
            len(effect_ids) == 1,
            duplicate_effects == 0,
            version_delta == 1,
            receipt is not None,
            receipt is not None
            and receipt.get("provider_source") == PROVIDER_SOURCE,
            receipt is not None
            and receipt.get("payload_hash") == run["payload_hash"],
            receipt is not None
            and receipt.get("order_version") == order["version"],
            receipt is not None and receipt.get("effect_id") in effect_ids,
            order["scheduled_for"] == run["scheduled_for"],
            bool(order["supplier_notified"]) == bool(run["notify_supplier"]),
            bool(final_step.get("verification", {}).get("passed")),
        ]
    )

    proof: dict[str, Any] = {
        "verdict": "PASSED" if passed else "FAILED",
        "claim": (
            "The agent diagnosed authoritative absence, retried once with the "
            "same key, and verified exactly one external effect."
            if used_safe_retry
            else "The committed external effect was recovered without repeating "
            "the mutation."
        ),
        "run_id": run_id,
        "scenario": run["scenario"],
        "idempotency_key": run["idempotency_key"],
        "agent_step_count": len(trace),
        "agent_diagnosis": final_step.get("diagnosis", {}).get("top_hypothesis"),
        "selected_strategy": final_step.get("strategy"),
        "safety_decision": final_step.get("policy", {}).get("decision"),
        "effect_ids_before_recovery": first_evidence.get("effect_ids", []),
        "effect_ids_after_recovery": effect_ids,
        "provider_mutation_requests_before_recovery": first_evidence.get(
            "provider_mutation_requests", 0
        ),
        "provider_mutation_requests_after_recovery": run[
            "provider_mutation_requests"
        ],
        "provider_commits": run["provider_commits"],
        "provider_available_during_proof": provider_available,
        "semantic_effect_count": len(effect_ids),
        "duplicate_effects": duplicate_effects,
        "order_version_before_action": run["order_version_before"],
        "order_version_after_recovery": order["version"],
        "version_delta": version_delta,
        "recovery_lookup_calls": run["recovery_lookup_calls"],
        "recovery_mutation_calls": run["recovery_mutation_calls"],
        "receipt_source": receipt.get("provider_source") if receipt else None,
        "receipt_payload_hash_matches": bool(
            receipt and receipt.get("payload_hash") == run["payload_hash"]
        ),
        "scheduled_for": order["scheduled_for"],
        "supplier_notified": bool(order["supplier_notified"]),
    }
    proof["proof_hash"] = "sha256-" + sha256_text(canonical_json(proof))
    return proof


def snapshot(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        timeline = [
            dict(row)
            for row in connection.execute(
                """
                SELECT event_type, detail, created_at
                FROM events WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        ]
    provider_status = "available"
    try:
        order = fetch_provider_order(run["order_id"])
        effects = list_provider_effects(run_id)
    except (ProviderUnavailable, ProviderNotFound):
        provider_status = "unavailable"
        order = {
            "order_id": run["order_id"],
            "scheduled_for": run["scheduled_for"],
            "supplier_notified": bool(run["notify_supplier"]),
            "version": run["order_version_after"]
            or run["order_version_before"],
        }
        effects = []
    trace = load_agent_trace(run_id)
    return {
        "run": serialize_run(run),
        "provider": {
            "status": provider_status,
            "mode": provider_mode(),
            "order": order,
            "effects": effects,
        },
        "timeline": timeline,
        "agent_trace": trace,
        "latest_reasoning": trace[-1] if trace else None,
        "proof": build_proof(run_id) if run["phase"] == "VERIFIED" else None,
    }


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD_PATH)


@app.get("/api")
def project_overview() -> dict[str, Any]:
    return {
        "project": "BOUNDARY Recovery Agent",
        "dashboard": "/",
        "challenge": "Agentic Recovery of a Failing System",
        "demo": (
            "The agent observes evidence, diagnoses the incident, selects a safe "
            "strategy, acts through an allow-listed tool, and verifies recovery."
        ),
        "scenarios": sorted(SUPPORTED_SCENARIOS),
        "provider_boundary": provider_mode(),
        "model": "/api/model",
        "docs": "/docs",
    }


@app.get("/api/model")
def get_model_card() -> dict[str, Any]:
    card = model_card()
    card["supported_scenarios"] = sorted(SUPPORTED_SCENARIOS)
    return card


@app.get("/health")
def health_check() -> dict[str, Any]:
    provider_status = "healthy"
    try:
        provider_health()
    except ProviderUnavailable:
        provider_status = "unavailable"
    return {
        "status": "healthy",
        "service": "boundary-recovery-agent",
        "provider": provider_status,
        "provider_mode": provider_mode(),
    }


@app.get("/ready")
def readiness_check() -> dict[str, Any]:
    try:
        provider = provider_health()
    except ProviderUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail="The independent provider service is unavailable",
        ) from error
    return {"status": "ready", "provider": provider}


@app.post("/api/demo/reset")
def reset_demo() -> dict[str, Any]:
    try:
        provider_result = reset_provider()
    except ProviderUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail="Provider reset failed; local evidence was preserved",
        ) from error
    with open_db() as connection:
        connection.executescript(
            """
            DELETE FROM recovery_leases;
            DELETE FROM agent_steps;
            DELETE FROM events;
            DELETE FROM runs;
            """
        )
    provider_order_state = provider_result.get("order", {})
    return {
        "status": "reset",
        "provider_order": {
            "order_id": provider_order_state.get("order_id", "731"),
            "scheduled_for": provider_order_state.get(
                "scheduled_for", "Thursday 09:00"
            ),
            "supplier_notified": bool(
                provider_order_state.get("supplier_notified", False)
            ),
            "version": provider_order_state.get("version", 12),
        },
    }


@app.post("/api/runs", status_code=201)
def create_run(request: CreateRunRequest) -> dict[str, Any]:
    if request.scenario not in SUPPORTED_SCENARIOS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unsupported scenario. Choose one of: "
                + ", ".join(sorted(SUPPORTED_SCENARIOS))
            ),
        )
    run_id = uuid.uuid4().hex[:8]
    operation_id = f"OP-{run_id.upper()}"
    idempotency_key = f"boundary:{run_id}:reschedule"
    payload = {
        "action": "reschedule_order",
        "order_id": request.order_id,
        "scheduled_for": request.scheduled_for,
        "notify_supplier": request.notify_supplier,
    }
    payload_json = canonical_json(payload)
    payload_hash = sha256_text(payload_json)
    with open_db() as connection:
        order = provider_order(connection, request.order_id)
        created_at = utc_now()
        connection.execute(
            """
            INSERT INTO runs
                (id, scenario, phase, connection, order_id, scheduled_for,
                 notify_supplier, operation_id, idempotency_key, payload_json,
                 payload_hash, effect_status, order_version_before,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                request.scenario,
                "READY",
                "CONNECTED",
                request.order_id,
                request.scheduled_for,
                int(request.notify_supplier),
                operation_id,
                idempotency_key,
                payload_json,
                payload_hash,
                "REQUEST_PREPARED",
                order["version"],
                created_at,
                created_at,
            ),
        )
        add_event(
            connection,
            run_id,
            "REQUEST_PERSISTED",
            (
                f"Persisted {operation_id} with idempotency key "
                f"{idempotency_key} before dispatch."
            ),
        )
    return snapshot(run_id)


@app.post("/api/runs/{run_id}/execute")
def execute_run(run_id: str) -> JSONResponse:
    with open_db() as connection:
        get_run_row(connection, run_id)
        cursor = connection.execute(
            """
            UPDATE runs
            SET phase = 'EXECUTING', effect_status = 'DISPATCHED', updated_at = ?
            WHERE id = ? AND phase = 'READY'
            """,
            (utc_now(), run_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="Only a READY run can execute. Recover incidents instead.",
            )
        scenario = get_run_row(connection, run_id)["scenario"]
        add_event(
            connection,
            run_id,
            "DISPATCHED",
            "Sent the persisted operation toward the external provider.",
        )

    committed = scenario in {"ack_lost", "receipt_conflict", "compound"}
    if committed:
        provider_commit(run_id)

    with open_db() as connection:
        run = get_run_row(connection, run_id)
        idempotency_key = run["idempotency_key"]

    if scenario == "provider_unavailable":
        configure_provider_fault(
            idempotency_key, lookup_failures_remaining=-1
        )
    elif scenario == "compound":
        configure_provider_fault(
            idempotency_key, lookup_failures_remaining=2
        )
    elif scenario == "receipt_conflict":
        configure_provider_fault(idempotency_key, tamper_receipt=True)
        with open_db() as connection:
            add_event(
                connection,
                run_id,
                "RECEIPT_TAMPERED",
                (
                    "The provider boundary fault injector will return a "
                    "conflicting receipt hash."
                ),
            )

    status_by_scenario = {
        "ack_lost": "ACK_LOST",
        "receipt_conflict": "ACK_LOST",
        "compound": "ACK_LOST",
        "no_commit": "REQUEST_LOST",
        "provider_unavailable": "PROVIDER_TIMEOUT",
    }
    reality = (
        "COMMITTED"
        if committed
        else ("UNREACHABLE" if scenario == "provider_unavailable" else "NO_COMMIT")
    )
    with open_db() as connection:
        connection.execute(
            """
            UPDATE runs
            SET phase = 'INCIDENT', connection = 'LOST', effect_status = ?,
                receipt_json = NULL, updated_at = ?
            WHERE id = ?
            """,
            (status_by_scenario[scenario], utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "INCIDENT_DETECTED",
            (
                "The durable request has no trusted local outcome. "
                "BOUNDARY must diagnose before any mutation can continue."
            ),
        )

    return JSONResponse(
        status_code=503,
        content={
            "code": "SIMULATED_UNCERTAIN_OUTCOME",
            "scenario": scenario,
            "run_id": run_id,
            "provider_reality": reality,
            "agent_knowledge": "OUTCOME_UNKNOWN",
            "next": f"POST /api/runs/{run_id}/recover",
        },
    )


@app.post("/api/runs/{run_id}/recover")
def recover_run(run_id: str) -> dict[str, Any]:
    run_recovery_agent(run_id)
    return snapshot(run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return snapshot(run_id)


@app.get("/api/runs/{run_id}/agent-trace")
def get_agent_trace(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        get_run_row(connection, run_id)
    return {"run_id": run_id, "steps": load_agent_trace(run_id)}


@app.get("/api/runs/{run_id}/proof")
def get_proof(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
    if run["phase"] != "VERIFIED":
        raise HTTPException(status_code=409, detail="Recovery is not verified yet")
    return build_proof(run_id)


@app.get("/api/provider/orders/{order_id}")
def get_provider_order(order_id: str) -> dict[str, Any]:
    try:
        return fetch_provider_order(order_id)
    except ProviderNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProviderUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/provider/receipts/{idempotency_key}")
def get_provider_receipt(idempotency_key: str) -> dict[str, Any]:
    try:
        receipt = lookup_provider_receipt(idempotency_key)
    except ProviderUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if receipt is None:
        raise HTTPException(status_code=404, detail="Provider receipt not found")
    return receipt
