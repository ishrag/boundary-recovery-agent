# BOUNDARY Recovery Agent

[![Tests](https://github.com/ishrag/boundary-recovery-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/ishrag/boundary-recovery-agent/actions/workflows/tests.yml)

> An evidence-driven recovery agent for external actions whose outcome becomes uncertain after a crash, timeout, or lost acknowledgement.

An AI agent can send an action to an external system, lose the response, and no longer know whether the action succeeded. Retrying blindly can duplicate an order, payment, notification, or workflow step. Stopping blindly can leave the requested work incomplete.

BOUNDARY turns that dangerous unknown into a controlled recovery decision. It preserves the original intent, observes authoritative provider evidence, diagnoses the incident, chooses an allow-listed strategy, applies a deterministic safety policy, and verifies the final state before declaring recovery.

## Why it matters

Distributed systems do not offer a single atomic transaction across an AI agent and an external provider. The difficult state is therefore not simply *success* or *failure*—it is **outcome unknown**.

BOUNDARY demonstrates a practical pattern for that boundary:

- persist operation identity and payload before dispatch;
- distinguish an outage from authoritative absence;
- reconcile by the original idempotency key;
- permit a retry only after the provider proves that no effect exists;
- quarantine conflicting evidence instead of forcing automation;
- verify the receipt, provider state, version change, and effect count.

## Challenge fit: Agentic Recovery of a Failing System

The project is deliberately focused on a working recovery loop rather than a broad monitoring concept.

| Challenge requirement | BOUNDARY implementation |
| --- | --- |
| Identify the incident | Detects a durable request with no trusted local outcome. |
| Diagnose likely causes | An interpretable categorical Naive Bayes model ranks five incident hypotheses from local and provider evidence. The scenario name is not a model feature. |
| Recommend or perform corrective action | The agent selects an allow-listed lookup, same-key retry, wait, or escalation tool. |
| Recover safely | A deterministic policy gate independently authorizes or blocks every tool; the model cannot directly approve a write. |
| Verify the result | The agent checks the provider receipt, final order state, version delta, semantic effect count, and duplicate count. |

## How the agent works

BOUNDARY follows an explicit **Observe → Diagnose → Plan → Act → Verify** loop:

1. **Observe** — read the durable request, local receipt state, provider receipt, external order state, effect count, transport signal, and lookup errors.
2. **Diagnose** — rank hypotheses with a local, interpretable classifier and expose confidence, margin, decisive evidence, and contradictory evidence.
3. **Plan** — map the leading diagnosis to an allow-listed strategy.
4. **Act** — pass the proposed tool through a deterministic safety gate. “Act” may mean a read-only lookup, a single same-key retry, waiting, or escalation.
5. **Verify** — compare the authoritative receipt and provider state with the persisted intent and reject duplicate or inconsistent effects.

The agent can perform at most three reasoning cycles in the MVP. Low-confidence or low-margin diagnoses fail closed.

## Architecture

```text
Browser dashboard
       |
       v
BOUNDARY FastAPI service :8000
  |-- durable intent, events, leases, reasoning trace --> boundary.db
  |-- local incident model
  |-- deterministic policy gate
  |-- allow-listed recovery tools
  |
  | HTTP through provider gateway
  v
Independent provider service :8001
  |-- idempotent operation endpoint
  |-- authoritative receipts, orders, effects --> provider.db
  |-- controlled fault injection for the demo
```

`run_demo.py` and Docker Compose run the provider as a separate HTTP process. The gateway also has a local adapter used by tests and lightweight development when `BOUNDARY_PROVIDER_URL` is not set.

## Enforced safety properties

These are properties of the current prototype and test suite, not claims of universal distributed exactly-once delivery:

- **Durable intent before dispatch** — operation ID, idempotency key, canonical payload, and payload hash are stored first.
- **Bound idempotency** — the provider returns the original receipt for the same key and rejects the key if it is reused for different work.
- **Scenario-blind diagnosis** — the classifier receives observed evidence, not the injected scenario label.
- **Independent authorization** — confidence and margin thresholds are checked before policy evaluation; every mutation requires deterministic evidence.
- **Outage is not absence** — provider unavailability never authorizes a retry.
- **Single recovery owner** — a database-backed lease prevents concurrent recovery agents from performing the same work.
- **Post-action verification** — verified runs require one intended semantic effect, no duplicates, the expected version delta, and a receipt bound to the persisted payload.
- **Auditable outcome** — each observation, diagnosis, policy decision, tool result, and verification is recorded; verified runs receive a deterministic proof fingerprint.

## Failure scenarios

The dashboard exposes five controlled scenarios against the same protected order action:

| Scenario | Evidence-driven decision | Expected terminal state |
| --- | --- | --- |
| `ack_lost` | The provider committed but the acknowledgement was lost. Reconcile the existing receipt without replaying the mutation. | `VERIFIED` |
| `no_commit` | The request never committed. After authoritative absence is proven, retry once with the original key. | `VERIFIED` |
| `provider_unavailable` | Provider evidence is unavailable. Wait and reobserve; never infer absence from an outage. | `BLOCKED` after the bounded recovery loop |
| `receipt_conflict` | The returned receipt conflicts with the persisted payload. Quarantine the evidence and escalate. | `BLOCKED` |
| `compound` | A committed action is temporarily unobservable. Reobserve until evidence becomes available, then reconcile it. | `VERIFIED` |

`BLOCKED` is a safe result: the policy gate refused to send an unverified external write.

## Live demo

1. Start both services with `run_demo.py` or Docker Compose.
2. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).
3. Select a failure condition and optionally change the requested schedule.
4. Choose **Create incident**. The demo deliberately returns an uncertain outcome; the dashboard then shows the difference between agent knowledge and provider reality.
5. Choose **Let agent recover**.
6. Inspect the diagnosis, confidence, decisive evidence, strategy, safety gate, provider state, machine-checkable proof, autonomous decision trace, and audit trail.

Useful endpoints:

- Dashboard: `GET /`
- API overview: `GET /api`
- Model card: `GET /api/model`
- Liveness: `GET /health`
- Provider-aware readiness: `GET /ready`
- OpenAPI documentation: `GET /docs`

## Local setup

Python 3.11 is the supported runtime used by the Docker image.

### Windows PowerShell

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_demo.py
```

The launcher starts the provider on `127.0.0.1:8001`, waits for it to become healthy, starts BOUNDARY on `127.0.0.1:8000`, and stops both processes when the run is stopped.

In PyCharm, select the project’s Python 3.11 virtual environment and run `run_demo.py`.

### macOS or Linux

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python run_demo.py
```

### Docker Compose

```bash
docker compose up --build
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Stop the services while preserving evidence volumes:

```bash
docker compose down
```

Delete the demo databases as well:

```bash
docker compose down --volumes
```

The default Compose binding is localhost. Copy `.env.example` to `.env` when you need to override `BOUNDARY_BIND` or `BOUNDARY_PORT` for Compose. The remaining values in that example document direct-process settings; `run_demo.py` uses shell environment variables and does not load `.env` automatically.

## Testing and validation

Run the complete suite:

```bash
python -m pytest -q
```

The suite covers:

- all five diagnosis and recovery scenarios;
- custom schedules, supplier notification, and invalid inputs;
- repeated and concurrent recovery;
- concurrent dispatch;
- idempotency-key conflicts;
- provider outage semantics;
- restart before and after provider commit;
- a real process crash followed by recovery in a fresh process;
- recovery across an independent HTTP provider process;
- dashboard and API availability.

### Publish the latest validation result

The dashboard does not contain a hard-coded test count. Generate a fresh validation artifact by running:

```bash
python publish_validation.py -q
```

This executes pytest and atomically writes `app/static/validation.json` only after pytest completes normally. The artifact records the collected result counts, pytest exit status and version, UTC timestamp, Git revision, and whether the working tree was dirty. The dashboard displays this as the **latest published automated run**; it is not continuous production monitoring.

## Project structure

```text
.
|-- app/
|   |-- main.py                 # Recovery API, agent loop, policy, proof, leases
|   |-- diagnosis.py            # Local interpretable incident classifier
|   |-- provider_gateway.py     # HTTP provider client and local test adapter
|   `-- static/
|       |-- index.html          # Judge-facing interactive dashboard
|       `-- validation.json     # Generated latest-test artifact
|-- provider_service/
|   |-- main.py                 # Independent provider HTTP API
|   `-- store.py                # Idempotent provider state and fault injection
|-- tests/                      # Unit, integration, concurrency, crash tests
|-- run_demo.py                 # Starts both local services and manages shutdown
|-- publish_validation.py       # Runs pytest and publishes validation.json
|-- Dockerfile
|-- docker-compose.yml
|-- requirements.txt            # Local development and testing
`-- requirements-runtime.txt    # Runtime container dependencies
```

## Current limitations

- The protected business action is a controlled order-reschedule example, and failures are deliberately injected.
- The diagnosis model is trained at startup from a small synthetic categorical case set; it has not been calibrated on production incident data.
- SQLite and the current recovery lease are suitable for a single-node MVP, not a horizontally scaled control plane.
- The agent loop is intentionally bounded to three steps and five hypotheses.
- Demo reset and fault-injection endpoints are not authenticated and must not be publicly exposed.
- The prototype does not yet provide tenant isolation, production secrets management, TLS termination, distributed tracing, alerting, or a human-review console.
- The proof verifies the invariants represented by this demo; it is not a formal proof of exactly-once delivery for arbitrary external systems.

## Path to production

1. Replace the demo provider with signed, authenticated adapters for real external systems and define an explicit reconciliation contract for each action.
2. Move durable intent, evidence, and leases to PostgreSQL or another transactional shared store; use distributed fencing for recovery ownership.
3. Add authentication, tenant isolation, authorization, secrets management, rate limits, and protected administrative endpoints.
4. Add structured logs, metrics, traces, alerts, replayable incident bundles, and a human escalation workflow.
5. Train and calibrate diagnosis on reviewed incident data while keeping deterministic authorization independent from the model.
6. Extend chaos, load, timeout, and partial-failure testing across multiple workers and regions.
7. Sign proof records and export them to an append-only audit system.

BOUNDARY’s central rule remains the same: **when the outcome is unknown, observe and reconcile before writing again.**
