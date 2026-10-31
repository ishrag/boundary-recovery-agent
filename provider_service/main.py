from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from provider_service.store import (
    ProviderConflict,
    ProviderNotFound,
    ProviderTemporarilyUnavailable,
    commit_operation,
    configure_fault,
    get_order,
    list_effects,
    lookup_receipt,
    provider_health,
    reset_store,
)


app = FastAPI(
    title="BOUNDARY Order Provider",
    description=(
        "An independent idempotent order service used to demonstrate a real "
        "external recovery boundary."
    ),
    version="1.0.0",
)


class CommitRequest(BaseModel):
    run_id: str
    operation_id: str
    idempotency_key: str
    payload_hash: str
    order_id: str
    scheduled_for: str
    notify_supplier: bool = False


class FaultRequest(BaseModel):
    lookup_failures_remaining: int = 0
    tamper_receipt: bool = False


@app.get("/health")
def health() -> dict[str, str]:
    return provider_health()


@app.post("/v1/internal/reset")
def reset() -> dict[str, Any]:
    return {"status": "reset", "order": reset_store()}


@app.post("/v1/internal/faults/{idempotency_key:path}")
def set_fault(
    idempotency_key: str, request: FaultRequest
) -> dict[str, Any]:
    configure_fault(
        idempotency_key,
        lookup_failures_remaining=request.lookup_failures_remaining,
        tamper_receipt=request.tamper_receipt,
    )
    return {"status": "configured"}


@app.post("/v1/operations")
def commit(request: CommitRequest) -> JSONResponse:
    try:
        result = commit_operation(request.model_dump())
    except ProviderConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProviderNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return JSONResponse(
        status_code=201 if result["created"] else 200,
        content=result,
    )


@app.get("/v1/receipts/{idempotency_key:path}")
def receipt(idempotency_key: str) -> dict[str, Any]:
    try:
        result = lookup_receipt(idempotency_key)
    except ProviderTemporarilyUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return result


@app.get("/v1/orders/{order_id}")
def order(order_id: str) -> dict[str, Any]:
    try:
        return get_order(order_id)
    except ProviderNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/v1/effects/{run_id}")
def effects(run_id: str) -> list[dict[str, Any]]:
    return list_effects(run_id)

