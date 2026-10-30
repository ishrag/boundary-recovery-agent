from fastapi.testclient import TestClient

from app.diagnosis import FEATURES, diagnose
from app.main import app


client = TestClient(app)


def run_scenario(scenario: str) -> dict:
    reset = client.post("/api/demo/reset")
    assert reset.status_code == 200
    created = client.post("/api/runs", json={"scenario": scenario})
    assert created.status_code == 201
    run_id = created.json()["run"]["id"]
    incident = client.post(f"/api/runs/{run_id}/execute")
    assert incident.status_code == 503
    recovered = client.post(f"/api/runs/{run_id}/recover")
    assert recovered.status_code == 200
    return recovered.json()


def test_model_is_local_interpretable_and_scenario_blind() -> None:
    response = client.get("/api/model")
    assert response.status_code == 200
    card = response.json()
    assert card["external_api_required"] is False
    assert card["training_case_count"] >= 15
    assert card["model_type"] == "Interpretable categorical Naive Bayes"
    assert "scenario" not in FEATURES
    assert "scenario" not in card["features"]


def test_model_probabilities_are_normalized_and_deterministic() -> None:
    evidence = {
        "request_persisted": "yes",
        "local_receipt_present": "no",
        "provider_reachable": "yes",
        "provider_operation": "present",
        "receipt_integrity": "match",
        "effect_count": "one",
        "version_delta": "one",
        "state_matches_intent": "yes",
        "transport_signal": "ack_lost",
        "lookup_error": "none",
    }
    first = diagnose(evidence)
    second = diagnose(evidence)
    assert first == second
    total = sum(item["probability"] for item in first["ranked_hypotheses"])
    assert abs(total - 1.0) < 0.00001
    assert first["top_hypothesis"] == "COMMITTED_ACK_LOST"


def test_ack_lost_is_reconciled_without_replay() -> None:
    data = run_scenario("ack_lost")
    assert data["run"]["phase"] == "VERIFIED"
    assert data["latest_reasoning"]["diagnosis"]["top_hypothesis"] == (
        "COMMITTED_ACK_LOST"
    )
    assert data["latest_reasoning"]["tool_name"] == "reconcile_receipt"
    assert data["run"]["recovery_mutation_calls"] == 0
    assert data["proof"]["verdict"] == "PASSED"
    assert data["proof"]["semantic_effect_count"] == 1
    assert data["proof"]["duplicate_effects"] == 0


def test_no_commit_is_safely_retried_with_original_key() -> None:
    data = run_scenario("no_commit")
    assert data["run"]["phase"] == "VERIFIED"
    assert data["latest_reasoning"]["diagnosis"]["top_hypothesis"] == (
        "NOT_COMMITTED_REQUEST_LOST"
    )
    assert data["latest_reasoning"]["tool_name"] == "retry_same_key"
    assert data["latest_reasoning"]["policy"]["mutation_allowed"] is True
    assert data["run"]["recovery_mutation_calls"] == 1
    assert data["run"]["provider_mutation_requests"] == 1
    assert data["proof"]["verdict"] == "PASSED"
    assert data["proof"]["semantic_effect_count"] == 1
    assert data["proof"]["duplicate_effects"] == 0


def test_provider_unavailable_fails_closed_without_mutation() -> None:
    data = run_scenario("provider_unavailable")
    assert data["run"]["phase"] == "BLOCKED"
    assert data["run"]["effect_status"] == "PROVIDER_UNAVAILABLE"
    assert data["run"]["provider_mutation_requests"] == 0
    assert data["run"]["provider_commits"] == 0
    assert data["run"]["recovery_mutation_calls"] == 0
    assert len(data["agent_trace"]) == 3
    assert all(
        step["diagnosis"]["top_hypothesis"] == "PROVIDER_UNAVAILABLE"
        for step in data["agent_trace"]
    )
    assert all(
        step["policy"]["mutation_allowed"] is False
        for step in data["agent_trace"]
    )
    assert data["provider"]["effects"] == []


def test_conflicting_receipt_is_quarantined() -> None:
    data = run_scenario("receipt_conflict")
    assert data["run"]["phase"] == "BLOCKED"
    assert data["run"]["effect_status"] == "RECEIPT_QUARANTINED"
    assert data["latest_reasoning"]["diagnosis"]["top_hypothesis"] == (
        "RECEIPT_CONFLICT_OR_TAMPERED"
    )
    assert data["latest_reasoning"]["tool_name"] == (
        "quarantine_and_escalate"
    )
    assert data["run"]["recovery_mutation_calls"] == 0
    assert data["run"]["receipt"] is None
    assert len(data["provider"]["effects"]) == 1


def test_compound_outage_reobserves_then_reconciles() -> None:
    data = run_scenario("compound")
    assert data["run"]["phase"] == "VERIFIED"
    diagnoses = [
        step["diagnosis"]["top_hypothesis"]
        for step in data["agent_trace"]
    ]
    assert diagnoses == [
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_UNAVAILABLE",
        "COMMITTED_ACK_LOST",
    ]
    assert [step["tool_name"] for step in data["agent_trace"]] == [
        "wait_and_reobserve",
        "wait_and_reobserve",
        "reconcile_receipt",
    ]
    assert data["run"]["recovery_mutation_calls"] == 0
    assert data["proof"]["verdict"] == "PASSED"
    assert data["proof"]["agent_step_count"] == 3
    assert data["proof"]["duplicate_effects"] == 0


def test_recovery_is_idempotent_after_agent_verifies() -> None:
    data = run_scenario("ack_lost")
    run_id = data["run"]["id"]
    again = client.post(f"/api/runs/{run_id}/recover")
    assert again.status_code == 200
    repeated = again.json()
    assert repeated["run"]["provider_mutation_requests"] == 1
    assert repeated["run"]["provider_commits"] == 1
    assert repeated["run"]["recovery_mutation_calls"] == 0
    assert repeated["proof"]["verdict"] == "PASSED"
    assert repeated["proof"]["duplicate_effects"] == 0
