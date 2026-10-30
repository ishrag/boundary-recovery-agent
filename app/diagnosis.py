from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any


FEATURES = (
    "request_persisted",
    "local_receipt_present",
    "provider_reachable",
    "provider_operation",
    "receipt_integrity",
    "effect_count",
    "version_delta",
    "state_matches_intent",
    "transport_signal",
    "lookup_error",
)

HYPOTHESES = (
    "COMMITTED_ACK_LOST",
    "NOT_COMMITTED_REQUEST_LOST",
    "PROVIDER_UNAVAILABLE",
    "RECEIPT_CONFLICT_OR_TAMPERED",
    "DUPLICATE_OR_PARTIAL_EFFECT",
)


def case(label: str, **values: str) -> tuple[str, dict[str, str]]:
    evidence = {feature: "unknown" for feature in FEATURES}
    evidence.update(values)
    return label, evidence


TRAINING_CASES = [
    case("COMMITTED_ACK_LOST", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="match", effect_count="one", version_delta="one", state_matches_intent="yes", transport_signal="ack_lost", lookup_error="none"),
    case("COMMITTED_ACK_LOST", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="match", effect_count="one", version_delta="one", state_matches_intent="yes", transport_signal="timeout", lookup_error="none"),
    case("COMMITTED_ACK_LOST", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="match", effect_count="one", version_delta="one", state_matches_intent="yes", transport_signal="none", lookup_error="none"),
    case("NOT_COMMITTED_REQUEST_LOST", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="absent", receipt_integrity="unknown", effect_count="zero", version_delta="zero", state_matches_intent="no", transport_signal="request_lost", lookup_error="none"),
    case("NOT_COMMITTED_REQUEST_LOST", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="absent", receipt_integrity="unknown", effect_count="zero", version_delta="zero", state_matches_intent="no", transport_signal="timeout", lookup_error="none"),
    case("NOT_COMMITTED_REQUEST_LOST", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="absent", receipt_integrity="unknown", effect_count="zero", version_delta="zero", state_matches_intent="no", transport_signal="none", lookup_error="none"),
    case("PROVIDER_UNAVAILABLE", request_persisted="yes", local_receipt_present="no", provider_reachable="no", provider_operation="unknown", receipt_integrity="unknown", effect_count="unknown", version_delta="unknown", state_matches_intent="unknown", transport_signal="timeout", lookup_error="unavailable"),
    case("PROVIDER_UNAVAILABLE", request_persisted="yes", local_receipt_present="no", provider_reachable="no", provider_operation="unknown", receipt_integrity="unknown", effect_count="unknown", version_delta="unknown", state_matches_intent="unknown", transport_signal="ack_lost", lookup_error="unavailable"),
    case("PROVIDER_UNAVAILABLE", request_persisted="yes", local_receipt_present="no", provider_reachable="no", provider_operation="unknown", receipt_integrity="unknown", effect_count="unknown", version_delta="unknown", state_matches_intent="unknown", transport_signal="request_lost", lookup_error="unavailable"),
    case("RECEIPT_CONFLICT_OR_TAMPERED", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="mismatch", effect_count="one", version_delta="one", state_matches_intent="yes", transport_signal="ack_lost", lookup_error="integrity_conflict"),
    case("RECEIPT_CONFLICT_OR_TAMPERED", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="mismatch", effect_count="one", version_delta="one", state_matches_intent="no", transport_signal="timeout", lookup_error="integrity_conflict"),
    case("RECEIPT_CONFLICT_OR_TAMPERED", request_persisted="yes", local_receipt_present="yes", provider_reachable="yes", provider_operation="present", receipt_integrity="mismatch", effect_count="one", version_delta="one", state_matches_intent="yes", transport_signal="none", lookup_error="integrity_conflict"),
    case("DUPLICATE_OR_PARTIAL_EFFECT", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="match", effect_count="many", version_delta="many", state_matches_intent="yes", transport_signal="ack_lost", lookup_error="none"),
    case("DUPLICATE_OR_PARTIAL_EFFECT", request_persisted="yes", local_receipt_present="no", provider_reachable="yes", provider_operation="present", receipt_integrity="mismatch", effect_count="many", version_delta="many", state_matches_intent="no", transport_signal="timeout", lookup_error="integrity_conflict"),
    case("DUPLICATE_OR_PARTIAL_EFFECT", request_persisted="yes", local_receipt_present="yes", provider_reachable="yes", provider_operation="present", receipt_integrity="match", effect_count="many", version_delta="many", state_matches_intent="no", transport_signal="none", lookup_error="none"),
]


class CategoricalNaiveBayes:
    def __init__(self, alpha: float = 1.0) -> None:
        self.alpha = alpha
        self.label_counts: Counter[str] = Counter()
        self.value_counts: dict[str, dict[str, Counter[str]]] = defaultdict(
            lambda: defaultdict(Counter)
        )
        self.vocab: dict[str, set[str]] = defaultdict(set)
        self.total = 0

    def fit(self, rows: list[tuple[str, dict[str, str]]]) -> None:
        for label, evidence in rows:
            self.label_counts[label] += 1
            self.total += 1
            for feature in FEATURES:
                value = evidence.get(feature, "unknown")
                self.value_counts[label][feature][value] += 1
                self.vocab[feature].add(value)

    def _feature_log_probability(
        self, label: str, feature: str, value: str
    ) -> float:
        count = self.value_counts[label][feature][value]
        choices = len(self.vocab[feature] | {value})
        denominator = self.label_counts[label] + self.alpha * choices
        return math.log((count + self.alpha) / denominator)

    def predict(self, evidence: dict[str, str]) -> dict[str, Any]:
        scores: dict[str, float] = {}
        contributions: dict[str, dict[str, float]] = {}
        labels = tuple(self.label_counts)
        for label in labels:
            prior = (self.label_counts[label] + self.alpha) / (
                self.total + self.alpha * len(labels)
            )
            score = math.log(prior)
            contributions[label] = {}
            for feature in FEATURES:
                value = evidence.get(feature, "unknown")
                contribution = self._feature_log_probability(
                    label, feature, value
                )
                contributions[label][feature] = contribution
                score += contribution
            scores[label] = score

        maximum = max(scores.values())
        weights = {label: math.exp(score - maximum) for label, score in scores.items()}
        total_weight = sum(weights.values())
        probabilities = {
            label: weights[label] / total_weight for label in labels
        }
        ranked = sorted(
            probabilities.items(), key=lambda item: item[1], reverse=True
        )
        top_label, confidence = ranked[0]
        runner_up, runner_confidence = ranked[1]

        deltas = {
            feature: contributions[top_label][feature]
            - contributions[runner_up][feature]
            for feature in FEATURES
        }
        decisive = [
            f"{feature}={evidence.get(feature, 'unknown')}"
            for feature, delta in sorted(
                deltas.items(), key=lambda item: item[1], reverse=True
            )
            if delta > 0
        ][:5]
        contradictory = [
            f"{feature}={evidence.get(feature, 'unknown')}"
            for feature, delta in sorted(deltas.items(), key=lambda item: item[1])
            if delta < 0
        ][:3]

        return {
            "model": "laplace_categorical_naive_bayes",
            "top_hypothesis": top_label,
            "confidence": round(confidence, 6),
            "runner_up": runner_up,
            "margin": round(confidence - runner_confidence, 6),
            "ranked_hypotheses": [
                {"hypothesis": label, "probability": round(probability, 6)}
                for label, probability in ranked
            ],
            "decisive_evidence": decisive,
            "contradictory_evidence": contradictory,
        }


MODEL = CategoricalNaiveBayes()
MODEL.fit(TRAINING_CASES)


def diagnose(evidence: dict[str, str]) -> dict[str, Any]:
    return MODEL.predict(evidence)


def model_card() -> dict[str, Any]:
    return {
        "name": "BOUNDARY Incident Reasoner",
        "model_type": "Interpretable categorical Naive Bayes",
        "training": "Deterministic local training at startup",
        "external_api_required": False,
        "training_case_count": len(TRAINING_CASES),
        "features": list(FEATURES),
        "hypotheses": list(HYPOTHESES),
        "decision_thresholds": {
            "minimum_confidence": 0.75,
            "minimum_margin": 0.20,
        },
        "safety": (
            "The model proposes a diagnosis. A deterministic policy gate "
            "independently authorizes or blocks every recovery tool."
        ),
    }
