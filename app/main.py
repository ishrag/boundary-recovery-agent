from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = Path(__file__).resolve().parent.parent / "boundary.db"
PROVIDER_SOURCE = "mock-order-service"

app = FastAPI(
    title="BOUNDARY Recovery Agent",
    description=(
        "A crash-safe recovery layer that reconciles an AI agent's memory "
        "with authoritative external evidence before it resumes."
    ),
    version="0.2.0",
)


class CreateRunRequest(BaseModel):
    scenario: str = "ack_lost"
    order_id: str = "731"
    scheduled_for: str = "Friday 10:00"
    notify_supplier: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def open_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with open_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs
            (
                id
                TEXT
                PRIMARY
                KEY,
                scenario
                TEXT
                NOT
                NULL,
                phase
                TEXT
                NOT
                NULL,
                connection
                TEXT
                NOT
                NULL,
                order_id
                TEXT
                NOT
                NULL,
                scheduled_for
                TEXT
                NOT
                NULL,
                notify_supplier
                INTEGER
                NOT
                NULL,
                operation_id
                TEXT
                NOT
                NULL,
                idempotency_key
                TEXT
                NOT
                NULL
                UNIQUE,
                payload_json
                TEXT
                NOT
                NULL,
                payload_hash
                TEXT
                NOT
                NULL,
                effect_status
                TEXT
                NOT
                NULL,
                receipt_json
                TEXT,
                provider_mutation_requests
                INTEGER
                NOT
                NULL
                DEFAULT
                0,
                provider_commits
                INTEGER
                NOT
                NULL
                DEFAULT
                0,
                recovery_lookup_calls
                INTEGER
                NOT
                NULL
                DEFAULT
                0,
                recovery_mutation_calls
                INTEGER
                NOT
                NULL
                DEFAULT
                0,
                order_version_before
                INTEGER
                NOT
                NULL,
                order_version_after
                INTEGER,
                created_at
                TEXT
                NOT
                NULL,
                updated_at
                TEXT
                NOT
                NULL
            );

            CREATE TABLE IF NOT EXISTS provider_orders
            (
                order_id
                TEXT
                PRIMARY
                KEY,
                scheduled_for
                TEXT
                NOT
                NULL,
                supplier_notified
                INTEGER
                NOT
                NULL,
                version
                INTEGER
                NOT
                NULL
            );

            CREATE TABLE IF NOT EXISTS provider_operations
            (
                idempotency_key
                TEXT
                PRIMARY
                KEY,
                run_id
                TEXT
                NOT
                NULL,
                operation_id
                TEXT
                NOT
                NULL,
                payload_hash
                TEXT
                NOT
                NULL,
                receipt_json
                TEXT
                NOT
                NULL,
                created_at
                TEXT
                NOT
                NULL
            );

            CREATE TABLE IF NOT EXISTS provider_effects
            (
                effect_id
                TEXT
                PRIMARY
                KEY,
                run_id
                TEXT
                NOT
                NULL,
                idempotency_key
                TEXT
                NOT
                NULL
                UNIQUE,
                semantic_signature
                TEXT
                NOT
                NULL,
                order_id
                TEXT
                NOT
                NULL,
                scheduled_for
                TEXT
                NOT
                NULL
            );

            CREATE TABLE IF NOT EXISTS events
            (
                id
                INTEGER
                PRIMARY
                KEY
                AUTOINCREMENT,
                run_id
                TEXT
                NOT
                NULL,
                event_type
                TEXT
                NOT
                NULL,
                detail
                TEXT
                NOT
                NULL,
                created_at
                TEXT
                NOT
                NULL
            );
            """
        )
        connection.execute(
            """
            INSERT
            OR IGNORE INTO provider_orders
                (order_id, scheduled_for, supplier_notified, version)
            VALUES (?, ?, ?, ?)
            """,
            ("731", "Thursday 09:00", 0, 12),
        )


init_db()


def get_run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM runs WHERE id = ?",
        (run_id,),
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


def provider_order(connection: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM provider_orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Provider order not found")
    result = dict(row)
    result["supplier_notified"] = bool(result["supplier_notified"])
    return result


def semantic_signature(order_id: str, scheduled_for: str) -> str:
    return sha256_text(
        canonical_json(
            {
                "action": "reschedule_order",
                "order_id": order_id,
                "scheduled_for": scheduled_for,
            }
        )
    )


def provider_commit(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        connection.execute(
            """
            UPDATE runs
            SET provider_mutation_requests = provider_mutation_requests + 1,
                updated_at                 = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )

        existing = connection.execute(
            """
            SELECT *
            FROM provider_operations
            WHERE idempotency_key = ?
            """,
            (run["idempotency_key"],),
        ).fetchone()
        if existing is not None:
            if (
                    existing["operation_id"] != run["operation_id"]
                    or existing["payload_hash"] != run["payload_hash"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was reused with different work",
                )
            return json.loads(existing["receipt_json"])

        order = provider_order(connection, run["order_id"])
        next_version = order["version"] + 1
        effect_id = f"FX-{run_id.upper()}"
        receipt = {
            "outcome": "committed",
            "provider_source": PROVIDER_SOURCE,
            "operation_id": run["operation_id"],
            "idempotency_key": run["idempotency_key"],
            "payload_hash": run["payload_hash"],
            "order_id": run["order_id"],
            "scheduled_for": run["scheduled_for"],
            "supplier_notified": bool(run["notify_supplier"]),
            "order_version": next_version,
            "effect_id": effect_id,
        }

        connection.execute(
            """
            UPDATE provider_orders
            SET scheduled_for     = ?,
                supplier_notified = ?,
                version           = ?
            WHERE order_id = ?
            """,
            (
                run["scheduled_for"],
                run["notify_supplier"],
                next_version,
                run["order_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO provider_effects
            (effect_id, run_id, idempotency_key, semantic_signature,
             order_id, scheduled_for)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                effect_id,
                run_id,
                run["idempotency_key"],
                semantic_signature(run["order_id"], run["scheduled_for"]),
                run["order_id"],
                run["scheduled_for"],
            ),
        )
        connection.execute(
            """
            INSERT INTO provider_operations
            (idempotency_key, run_id, operation_id, payload_hash,
             receipt_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run["idempotency_key"],
                run_id,
                run["operation_id"],
                run["payload_hash"],
                canonical_json(receipt),
                utc_now(),
            ),
        )
        connection.execute(
            """
            UPDATE runs
            SET provider_commits    = provider_commits + 1,
                order_version_after = ?,
                updated_at          = ?
            WHERE id = ?
            """,
            (next_version, utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "PROVIDER_COMMITTED",
            (
                f"Provider changed order {run['order_id']} to "
                f"{run['scheduled_for']} at version {next_version}."
            ),
        )
        return receipt


def build_proof(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        order = provider_order(connection, run["order_id"])
        effects = connection.execute(
            """
            SELECT effect_id
            FROM provider_effects
            WHERE run_id = ?
            ORDER BY effect_id
            """,
            (run_id,),
        ).fetchall()

    effect_ids = [row["effect_id"] for row in effects]
    effect_count = len(effect_ids)
    duplicate_effects = max(0, effect_count - 1)
    version_after = order["version"]
    version_delta = version_after - run["order_version_before"]
    receipt = json.loads(run["receipt_json"]) if run["receipt_json"] else None

    passed = all(
        [
            run["phase"] == "VERIFIED",
            run["provider_mutation_requests"] == 1,
            run["provider_commits"] == 1,
            run["recovery_lookup_calls"] >= 1,
            run["recovery_mutation_calls"] == 0,
            effect_count == 1,
            duplicate_effects == 0,
            version_delta == 1,
            receipt is not None,
            receipt is not None
            and receipt.get("provider_source") == PROVIDER_SOURCE,

            receipt is not None
            and receipt.get("order_version") == order["version"],
            receipt is not None
            and receipt.get("effect_id") in effect_ids,
            order["scheduled_for"] == run["scheduled_for"],
            bool(order["supplier_notified"]) == bool(run["notify_supplier"]),
        ]
    )

    proof: dict[str, Any] = {
        "verdict": "PASSED" if passed else "FAILED",
        "claim": (
            "The committed external effect was recovered without repeating "
            "the mutation."
        ),
        "run_id": run_id,
        "idempotency_key": run["idempotency_key"],
        "effect_ids_before_recovery": effect_ids,
        "effect_ids_after_recovery": effect_ids,
        "provider_mutation_requests_before_recovery": 1
        if run["provider_commits"]
        else 0,
        "provider_mutation_requests_after_recovery": run[
            "provider_mutation_requests"
        ],
        "provider_commits": run["provider_commits"],
        "semantic_effect_count": effect_count,
        "duplicate_effects": duplicate_effects,
        "order_version_before_action": run["order_version_before"],
        "order_version_after_recovery": version_after,
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
        order = provider_order(connection, run["order_id"])
        effects = [
            dict(row)
            for row in connection.execute(
                """
                SELECT effect_id, idempotency_key, order_id, scheduled_for
                FROM provider_effects
                WHERE run_id = ?
                ORDER BY effect_id
                """,
                (run_id,),
            ).fetchall()
        ]
        timeline = [
            dict(row)
            for row in connection.execute(
                """
                SELECT event_type, detail, created_at
                FROM events
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        ]

    return {
        "run": serialize_run(run),
        "provider": {
            "order": order,
            "effects": effects,
        },
        "timeline": timeline,
        "proof": build_proof(run_id) if run["phase"] == "VERIFIED" else None,
    }


@app.get("/")
def project_overview() -> dict[str, Any]:
    return {
        "project": "BOUNDARY Recovery Agent",
        "challenge": "Agentic Recovery of a Failing System",
        "demo": (
            "An external service commits, the AI agent loses the receipt, "
            "and BOUNDARY reconciles instead of replaying the action."
        ),
        "steps": [
            "POST /api/demo/reset",
            "POST /api/runs",
            "POST /api/runs/{run_id}/execute",
            "POST /api/runs/{run_id}/recover",
            "GET /api/runs/{run_id}/proof",
        ],
        "docs": "/docs",
    }


@app.get("/health")
def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "boundary-recovery-agent",
    }


@app.post("/api/demo/reset")
def reset_demo() -> dict[str, Any]:
    with open_db() as connection:
        connection.executescript(
            """
            DELETE
            FROM events;
            DELETE
            FROM provider_effects;
            DELETE
            FROM provider_operations;
            DELETE
            FROM runs;
            DELETE
            FROM provider_orders;
            """
        )
        connection.execute(
            """
            INSERT INTO provider_orders
                (order_id, scheduled_for, supplier_notified, version)
            VALUES (?, ?, ?, ?)
            """,
            ("731", "Thursday 09:00", 0, 12),
        )
    return {
        "status": "reset",
        "provider_order": {
            "order_id": "731",
            "scheduled_for": "Thursday 09:00",
            "supplier_notified": False,
            "version": 12,
        },
    }


@app.post("/api/runs", status_code=201)
def create_run(request: CreateRunRequest) -> dict[str, Any]:
    if request.scenario != "ack_lost":
        raise HTTPException(
            status_code=422,
            detail="The MVP currently supports the ack_lost scenario.",
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
            INSERT INTO runs (id, scenario, phase, connection, order_id, scheduled_for,
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
        run = get_run_row(connection, run_id)
        if run["phase"] != "READY":
            raise HTTPException(
                status_code=409,
                detail="Only a READY run can execute. Recover incidents instead.",
            )
        connection.execute(
            """
            UPDATE runs
            SET phase         = 'EXECUTING',
                effect_status = 'DISPATCHED',
                updated_at    = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "DISPATCHED",
            "Sent the persisted operation to the external provider.",
        )

    provider_commit(run_id)

    with open_db() as connection:
        connection.execute(
            """
            UPDATE runs
            SET phase         = 'INCIDENT',
                connection    = 'LOST',
                effect_status = 'OUTCOME_UNKNOWN',
                receipt_json  = NULL,
                updated_at    = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "ACK_LOST",
            (
                "The provider committed, but the agent crashed before "
                "persisting the receipt. Outcome is unknown locally."
            ),
        )

    return JSONResponse(
        status_code=503,
        content={
            "code": "SIMULATED_ACK_LOSS",
            "run_id": run_id,
            "provider_reality": "COMMITTED",
            "agent_knowledge": "OUTCOME_UNKNOWN",
            "next": f"POST /api/runs/{run_id}/recover",
        },
    )


@app.post("/api/runs/{run_id}/recover")
def recover_run(run_id: str) -> dict[str, Any]:
    with open_db() as connection:
        run = get_run_row(connection, run_id)
        if run["phase"] == "VERIFIED":
            return snapshot(run_id)
        if run["phase"] not in {"INCIDENT", "BLOCKED"}:
            raise HTTPException(
                status_code=409,
                detail="This run has no recoverable incident.",
            )

        connection.execute(
            """
            UPDATE runs
            SET phase                 = 'DIAGNOSING',
                connection            = 'RECONNECTING',
                recovery_lookup_calls = recovery_lookup_calls + 1,
                updated_at            = ?
            WHERE id = ?
            """,
            (utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "RECONCILIATION_STARTED",
            (
                "BOUNDARY queried the provider by the original idempotency "
                "key instead of repeating the mutation."
            ),
        )

        provider_operation = connection.execute(
            """
            SELECT *
            FROM provider_operations
            WHERE idempotency_key = ?
            """,
            (run["idempotency_key"],),
        ).fetchone()

        if provider_operation is None:
            connection.execute(
                """
                UPDATE runs
                SET phase         = 'BLOCKED',
                    connection    = 'CONNECTED',
                    effect_status = 'OUTCOME_UNKNOWN',
                    updated_at    = ?
                WHERE id = ?
                """,
                (utc_now(), run_id),
            )
            add_event(
                connection,
                run_id,
                "RECOVERY_BLOCKED",
                "No authoritative receipt was available; BOUNDARY failed closed.",
            )
            connection.commit()
            return snapshot(run_id)

        receipt = json.loads(provider_operation["receipt_json"])
        expected = {
            "outcome": "committed",
            "provider_source": PROVIDER_SOURCE,
            "operation_id": run["operation_id"],
            "idempotency_key": run["idempotency_key"],
            "payload_hash": run["payload_hash"],
            "order_id": run["order_id"],
            "scheduled_for": run["scheduled_for"],
            "supplier_notified": bool(run["notify_supplier"]),
        }
        mismatches = [
            field
            for field, expected_value in expected.items()
            if receipt.get(field) != expected_value
        ]

        if mismatches:
            connection.execute(
                """
                UPDATE runs
                SET phase         = 'BLOCKED',
                    connection    = 'CONNECTED',
                    effect_status = 'RECEIPT_REJECTED',
                    updated_at    = ?
                WHERE id = ?
                """,
                (utc_now(), run_id),
            )
            add_event(
                connection,
                run_id,
                "RECEIPT_REJECTED",
                "Receipt mismatch in: " + ", ".join(mismatches),
            )

            connection.commit()
            return snapshot(run_id)

        connection.execute(
            """
            UPDATE runs
            SET phase         = 'VERIFIED',
                connection    = 'CONNECTED',
                effect_status = 'VERIFIED',
                receipt_json  = ?,
                updated_at    = ?
            WHERE id = ?
            """,
            (canonical_json(receipt), utc_now(), run_id),
        )
        add_event(
            connection,
            run_id,
            "RECOVERY_VERIFIED",
            (
                "Authoritative receipt matched the persisted request. "
                "No second provider mutation was sent."
            ),
        )

    return snapshot(run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return snapshot(run_id)


@app.get("/api/runs/{run_id}/proof")
def get_proof(run_id: str) -> dict[str, Any]:
    return build_proof(run_id)


@app.get("/api/provider/orders/{order_id}")
def get_provider_order(order_id: str) -> dict[str, Any]:
    with open_db() as connection:
        return provider_order(connection, order_id)


@app.get("/api/provider/receipts/{idempotency_key}")
def get_provider_receipt(idempotency_key: str) -> dict[str, Any]:
    with open_db() as connection:
        row = connection.execute(
            """
            SELECT receipt_json
            FROM provider_operations
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Provider receipt not found")
    return json.loads(row["receipt_json"])
