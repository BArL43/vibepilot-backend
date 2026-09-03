# VibePilot API

VibePilot is a budget-aware orchestration layer for the VibeMarketolog Agent API. It turns a business brief into a guarded multi-step generation workflow: fixes a spending envelope, re-checks prices before paid actions, applies fallbacks when a step becomes too expensive, and produces a cryptographically verifiable execution receipt.

Current product version: `0.6.2`.

The repository contains a FastAPI backend, SQL persistence, an operator web interface, tests, architecture notes and a live-run verification workflow. The production entrypoint is `app.server:app`; it keeps the tested v0.5 execution engine and adds the v0.6 product layer: text-safe media prompts, deterministic Russian copy overlays, Budget Booster, campaign history and the canonical frontend.

## What the project demonstrates

- budget-aware orchestration of several paid AI generation steps;
- live price estimation immediately before a paid call;
- approval gates for expensive steps and runtime price drift;
- model fallback and optional-step skipping instead of overspending;
- idempotency keys and request fingerprints before generation;
- async completion through signed webhooks with polling fallback;
- SQLite/PostgreSQL persistence with optimistic revisions;
- reconciliation of generation costs/refunds against balance changes;
- Ed25519-signed execution receipts that can be verified offline;
- an operator UI and persistent campaign history;
- explicit separation between safe `demo` and spend-capable `live` modes.

## Core invariants

- `demo` never becomes `live` automatically, even when an API token is configured.
- Models are selected from `GET /capabilities` rather than from a hardcoded production catalog.
- Live price is taken from `POST /generate/estimate`, then checked again before the paid request.
- Approval is required when `estimate > threshold` or price drift exceeds the configured tolerance.
- When the envelope is insufficient, VibePilot tries a cheaper compatible model or an allowed `skip`; it does not overspend as a fallback.
- `actual_spend_rub` changes only from upstream `cost`; refunds are tracked separately.
- Async image/video steps are not marked complete until a platform status or signed webhook confirms completion.
- Before `POST /generate`, the workflow stores a request fingerprint, `running` state and deterministic `X-Idempotency-Key`.
- Live routes require a separate `X-VibePilot-Live-Key`, so a public frontend cannot spend the server balance without operator authorization.

## Demo vs live

| Mode | Price source | Paid generation | Receipt |
| --- | --- | ---: | --- |
| `demo` | indicative catalog values | no | simulation, spend `0` |
| `live` | exact Vibe estimate for the payload | yes | generation IDs and actual cost |

Demo mode exists for the interface and safe review. It does not pretend that a simulated run is a paid execution.

## Execution flow

1. A text model creates several campaign concepts.
2. Another available model evaluates them using a structured rubric.
3. The selected visual prompt is sent to an image model.
4. Before each paid step VibePilot refreshes the estimate and applies fallback or approval rules.
5. Generated media is kept text-safe; exact Russian copy is rendered as a deterministic overlay.
6. The generated image can be passed to an image-to-video model using the model-specific payload.
7. Async completion is accepted from an HMAC-SHA256 webhook, with polling as a fallback.
8. SQL state records the workflow and reconciliation facts.
9. Receipt v2 is signed with Ed25519 and can be checked offline.
10. After a completed campaign, Budget Booster can spend only the safe free part of the envelope on an additional A/B banner.

More detail: [architecture](docs/ARCHITECTURE.md), [product RFC](docs/RFC-001-budget-contract.md), [evals](docs/EVALS.md), [review guide](docs/REVIEW_GUIDE.md).

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.server:app --reload
```

Swagger: `http://localhost:8000/docs`.

Environment variables from `.env` must be loaded before startup; `.env` itself is ignored by Git.

## Main API routes

- `GET /api/v1/product` - current product metadata.
- `GET /api/v1/campaigns` - recent campaign history for an authorized operator.
- `POST /api/v1/budget/compare` - compare outcomes for several envelopes.
- `POST /api/v1/contracts/compile` - create an immutable Budget Contract.
- `POST /api/v1/contracts/{id}/activate` - create a workflow without spending.
- `POST /api/v1/workflows/{id}/execute` - execute until an async step or approval gate.
- `POST /api/v1/workflows/{id}/refresh` - polling fallback and continuation.
- `POST /api/v1/workflows/{id}/approve` - human decision for a gated step.
- `POST /api/v1/workflows/{id}/reconcile` - repeat balance/cost reconciliation.
- `GET /api/v1/workflows/{id}/creative` - assembled creative package and deterministic copy.
- `GET|POST /api/v1/workflows/{id}/booster` - inspect or run an additional safe-budget banner.
- `POST /api/v1/webhooks/vibe` - HMAC-verified callback.
- `POST /api/v1/integrations/vibe/webhook-test` - free end-to-end webhook self-test.
- `GET /api/v1/workflows/{id}/receipt` - signed execution evidence.
- `POST /api/v1/receipts/verify` - verify a receipt without workflow access.
- `GET /api/v1/receipts/public-key` - deployment public key.

Live operations require:

```text
X-VibePilot-Live-Key: <VIBEPILOT_LIVE_KEY>
```

The VibeMarketolog API token is never sent to the browser.

## Persistence and deployment

SQLAlchemy uses SQLite by default and PostgreSQL through `DATABASE_URL` in deployment. Optimistic revisions prevent a stale worker from overwriting newer workflow state; a separate index links `generation_id` to workflow/step.

The Render configuration starts `uvicorn app.server:app`. Production configuration expects:

- `VIBE_API_TOKEN` - Agent API key with the required scopes;
- `VIBEPILOT_LIVE_KEY` - independent operator secret;
- `PUBLIC_BASE_URL` - public HTTPS backend URL without a trailing slash;
- `VIBE_WEBHOOK_SECRET` - webhook signature secret;
- `RECEIPT_SIGNING_KEY` - stable Ed25519 private key;
- `DATABASE_URL` - PostgreSQL URL for persistent production state.

Without live secrets, the service stays in safe demo mode and reports that state through `/health`. It does not fake upstream generation. A real low-cost verification procedure is documented in [LIVE_RUN.md](LIVE_RUN.md).

## Verification

```bash
ruff check app scripts tests
ruff format --check app scripts tests
pytest -q
```

The suite covers fail-closed live behavior, threshold/drift policies, fallback, scope reduction, idempotency, signed webhooks, persistence/concurrency, reconciliation, Ed25519 receipts, v0.6 planner behavior, campaign history and the composed production server entrypoint.

CI runs the same lint, formatting, compile and test checks on every push and pull request.

## Evidence boundary

Code, contract tests and free `/capabilities` checks can be reviewed without a user token. The repository intentionally does not contain a fabricated `evidence/live-receipt.json`: that artifact should exist only after a real paid run. The instructions in `LIVE_RUN.md` preserve that boundary instead of substituting demo data for execution evidence.
