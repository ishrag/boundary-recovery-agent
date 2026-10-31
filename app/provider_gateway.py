from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

from provider_service import store


class ProviderUnavailable(Exception):
    """The provider outcome is unknown because the service could not answer."""


class ProviderConflict(Exception):
    """The provider rejected a reused idempotency key."""


class ProviderNotFound(Exception):
    """A requested provider resource does not exist."""


def provider_base_url() -> str | None:
    value = os.getenv("BOUNDARY_PROVIDER_URL", "").strip().rstrip("/")
    return value or None


def provider_mode() -> str:
    return "http" if provider_base_url() else "local-adapter"


def _timeout() -> float:
    return float(os.getenv("BOUNDARY_PROVIDER_TIMEOUT", "2.0"))


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    base_url = provider_base_url()
    if base_url is None:
        raise RuntimeError("HTTP provider mode is not enabled")
    try:
        return httpx.request(
            method,
            base_url + path,
            json=json_body,
            timeout=_timeout(),
        )
    except (httpx.TimeoutException, httpx.NetworkError) as error:
        raise ProviderUnavailable(str(error)) from error


def reset_provider() -> dict[str, Any]:
    if provider_base_url() is None:
        return {"status": "reset", "order": store.reset_store()}
    response = _request("POST", "/v1/internal/reset")
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()
    return response.json()


def configure_provider_fault(
    idempotency_key: str,
    *,
    lookup_failures_remaining: int = 0,
    tamper_receipt: bool = False,
) -> None:
    if provider_base_url() is None:
        store.configure_fault(
            idempotency_key,
            lookup_failures_remaining=lookup_failures_remaining,
            tamper_receipt=tamper_receipt,
        )
        return
    response = _request(
        "POST",
        "/v1/internal/faults/" + quote(idempotency_key, safe=""),
        json_body={
            "lookup_failures_remaining": lookup_failures_remaining,
            "tamper_receipt": tamper_receipt,
        },
    )
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()


def commit_provider_operation(request: dict[str, Any]) -> dict[str, Any]:
    if provider_base_url() is None:
        try:
            return store.commit_operation(request)
        except store.ProviderConflict as error:
            raise ProviderConflict(str(error)) from error
        except store.ProviderNotFound as error:
            raise ProviderNotFound(str(error)) from error

    response = _request("POST", "/v1/operations", json_body=request)
    if response.status_code == 409:
        raise ProviderConflict(response.text)
    if response.status_code == 404:
        raise ProviderNotFound(response.text)
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()
    return response.json()


def lookup_provider_receipt(
    idempotency_key: str,
) -> dict[str, Any] | None:
    if provider_base_url() is None:
        try:
            return store.lookup_receipt(idempotency_key)
        except store.ProviderTemporarilyUnavailable as error:
            raise ProviderUnavailable(str(error)) from error

    response = _request(
        "GET", "/v1/receipts/" + quote(idempotency_key, safe="")
    )
    if response.status_code == 404:
        return None
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()
    return response.json()


def get_provider_order(order_id: str) -> dict[str, Any]:
    if provider_base_url() is None:
        try:
            return store.get_order(order_id)
        except store.ProviderNotFound as error:
            raise ProviderNotFound(str(error)) from error
    response = _request("GET", "/v1/orders/" + quote(order_id, safe=""))
    if response.status_code == 404:
        raise ProviderNotFound(response.text)
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()
    return response.json()


def list_provider_effects(run_id: str) -> list[dict[str, Any]]:
    if provider_base_url() is None:
        return store.list_effects(run_id)
    response = _request("GET", "/v1/effects/" + quote(run_id, safe=""))
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()
    return response.json()


def provider_health() -> dict[str, Any]:
    if provider_base_url() is None:
        return store.provider_health()
    response = _request("GET", "/health")
    if response.status_code >= 500:
        raise ProviderUnavailable(response.text)
    response.raise_for_status()
    return response.json()

