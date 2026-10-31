from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROVIDER_SOURCE = "boundary-order-provider"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ProviderConflict(Exception):
    """The same idempotency key was reused for different work."""


class ProviderNotFound(Exception):
    """The requested provider resource does not exist."""


class ProviderTemporarilyUnavailable(Exception):
    """A demo fault made authoritative provider evidence unavailable."""


def provider_db_path() -> Path:
    return Path(
        os.getenv(
            "BOUNDARY_PROVIDER_DB_PATH",
            str(REPOSITORY_ROOT / "provider.db"),
        )
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def semantic_signature(order_id: str, scheduled_for: str) -> str:
    encoded = canonical_json(
        {
            "action": "reschedule_order",
            "order_id": order_id,
            "scheduled_for": scheduled_for,
        }
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def open_provider_db() -> sqlite3.Connection:
    path = provider_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_orders
        (
            order_id TEXT PRIMARY KEY,
            scheduled_for TEXT NOT NULL,
            supplier_notified INTEGER NOT NULL,
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_operations
        (
            idempotency_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_effects
        (
            effect_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            semantic_signature TEXT NOT NULL,
            order_id TEXT NOT NULL,
            scheduled_for TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_faults
        (
            idempotency_key TEXT PRIMARY KEY,
            lookup_failures_remaining INTEGER NOT NULL DEFAULT 0,
            tamper_receipt INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO provider_orders
            (order_id, scheduled_for, supplier_notified, version)
        VALUES (?, ?, ?, ?)
        """,
        ("731", "Thursday 09:00", 0, 12),
    )
    connection.commit()
    return connection


def reset_store() -> dict[str, Any]:
    with open_provider_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executescript(
            """
            DELETE FROM provider_faults;
            DELETE FROM provider_effects;
            DELETE FROM provider_operations;
            DELETE FROM provider_orders;
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
    return get_order("731")


def get_order(order_id: str) -> dict[str, Any]:
    with open_provider_db() as connection:
        row = connection.execute(
            "SELECT * FROM provider_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if row is None:
        raise ProviderNotFound(f"Provider order {order_id} was not found")
    result = dict(row)
    result["supplier_notified"] = bool(result["supplier_notified"])
    return result


def list_effects(run_id: str) -> list[dict[str, Any]]:
    with open_provider_db() as connection:
        rows = connection.execute(
            """
            SELECT effect_id, idempotency_key, order_id, scheduled_for
            FROM provider_effects
            WHERE run_id = ?
            ORDER BY effect_id
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def configure_fault(
    idempotency_key: str,
    *,
    lookup_failures_remaining: int = 0,
    tamper_receipt: bool = False,
) -> None:
    with open_provider_db() as connection:
        connection.execute(
            """
            INSERT INTO provider_faults
                (idempotency_key, lookup_failures_remaining, tamper_receipt)
            VALUES (?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                lookup_failures_remaining = excluded.lookup_failures_remaining,
                tamper_receipt = excluded.tamper_receipt
            """,
            (
                idempotency_key,
                lookup_failures_remaining,
                int(tamper_receipt),
            ),
        )


def lookup_receipt(idempotency_key: str) -> dict[str, Any] | None:
    with open_provider_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        fault = connection.execute(
            "SELECT * FROM provider_faults WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if fault is not None:
            remaining = int(fault["lookup_failures_remaining"])
            if remaining == -1:
                connection.commit()
                raise ProviderTemporarilyUnavailable(
                    "Provider lookup is unavailable"
                )
            if remaining > 0:
                connection.execute(
                    """
                    UPDATE provider_faults
                    SET lookup_failures_remaining = lookup_failures_remaining - 1
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                )
                connection.commit()
                raise ProviderTemporarilyUnavailable(
                    "Provider lookup is temporarily unavailable"
                )

        operation = connection.execute(
            """
            SELECT receipt_json FROM provider_operations
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if operation is None:
            return None
        receipt = json.loads(operation["receipt_json"])
        if fault is not None and bool(fault["tamper_receipt"]):
            receipt["payload_hash"] = (
                "tampered-" + str(receipt["payload_hash"])[:12]
            )
        return receipt


def commit_operation(request: dict[str, Any]) -> dict[str, Any]:
    required = {
        "run_id",
        "operation_id",
        "idempotency_key",
        "payload_hash",
        "order_id",
        "scheduled_for",
        "notify_supplier",
    }
    missing = sorted(required - request.keys())
    if missing:
        raise ValueError("Missing provider fields: " + ", ".join(missing))

    with open_provider_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT * FROM provider_operations WHERE idempotency_key = ?
            """,
            (request["idempotency_key"],),
        ).fetchone()
        if existing is not None:
            if (
                existing["operation_id"] != request["operation_id"]
                or existing["payload_hash"] != request["payload_hash"]
            ):
                raise ProviderConflict(
                    "Idempotency key was reused with different work"
                )
            return {
                "created": False,
                "receipt": json.loads(existing["receipt_json"]),
            }

        order = connection.execute(
            "SELECT * FROM provider_orders WHERE order_id = ?",
            (request["order_id"],),
        ).fetchone()
        if order is None:
            raise ProviderNotFound(
                f"Provider order {request['order_id']} was not found"
            )

        next_version = int(order["version"]) + 1
        effect_id = f"FX-{str(request['run_id']).upper()}"
        receipt = {
            "outcome": "committed",
            "provider_source": PROVIDER_SOURCE,
            "operation_id": request["operation_id"],
            "idempotency_key": request["idempotency_key"],
            "payload_hash": request["payload_hash"],
            "order_id": request["order_id"],
            "scheduled_for": request["scheduled_for"],
            "supplier_notified": bool(request["notify_supplier"]),
            "order_version": next_version,
            "effect_id": effect_id,
        }
        connection.execute(
            """
            UPDATE provider_orders
            SET scheduled_for = ?, supplier_notified = ?, version = ?
            WHERE order_id = ?
            """,
            (
                request["scheduled_for"],
                int(bool(request["notify_supplier"])),
                next_version,
                request["order_id"],
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
                request["run_id"],
                request["idempotency_key"],
                semantic_signature(
                    request["order_id"], request["scheduled_for"]
                ),
                request["order_id"],
                request["scheduled_for"],
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
                request["idempotency_key"],
                request["run_id"],
                request["operation_id"],
                request["payload_hash"],
                canonical_json(receipt),
                utc_now(),
            ),
        )
    return {"created": True, "receipt": receipt}


def provider_health() -> dict[str, str]:
    with open_provider_db() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "healthy", "service": "boundary-order-provider"}

