from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models import Workflow
from app.receipts import ReceiptSigner
from app.store import StateStore, StoreConflict
from app.vibe_client import VibeAPIError, VibeResponse

CATALOG: dict[str, Any] = {
    "status": "ok",
    "text_models": {
        "models": {
            "claude-opus-5": {
                "sample_cost_rub": 3.75,
                "min_charge_rub": 2,
            },
            "gpt-5.6-sol": {
                "sample_cost_rub": 4.2,
                "min_charge_rub": 2,
            },
        }
    },
    "models": {
        "text": {},
        "image": {
            "z-image": {
                "price": 1.2,
                "required": ["prompt"],
                "optional": ["aspect_ratio"],
            },
            "nano-banana-2-lite": {
                "price": 9,
                "required": ["prompt"],
                "optional": ["aspect_ratio", "image_input"],
            },
            "seedream-5-pro": {
                "price": 40,
                "required": ["prompt"],
                "optional": ["aspect_ratio", "quality"],
            },
        },
        "video": {
            "pixverse-v6": {
                "price": 40,
                "required": ["prompt"],
                "optional": [
                    "pv_mode",
                    "duration",
                    "resolution",
                    "aspect_ratio",
                    "generate_audio",
                    "image_urls",
                ],
            }
        },
        "voice": {},
        "music": {},
    },
}


class FakeVibeClient:
    def __init__(self) -> None:
        self.generate_calls: list[tuple[dict[str, Any], str]] = []
        self.estimate_calls: list[dict[str, Any]] = []
        self.estimate_overrides: dict[str, float] = {}
        self.prompt_limits: dict[str, int] = {}
        self.invalid_estimate_models: set[str] = set()
        self.selection_text_override: str | None = None
        self.fail_on_model: str | None = None
        self._generation_counter = 100
        self._generation_costs: dict[int, float] = {}
        self._generation_types: dict[int, str] = {}
        self.balance_rub = 500.0

    async def capabilities(self) -> VibeResponse:
        return VibeResponse(CATALOG, "req_capabilities", 200)

    async def me(self) -> VibeResponse:
        return VibeResponse({"scopes": ["read", "generate"]}, "req_me", 200)

    async def balance(self) -> VibeResponse:
        return VibeResponse({"balance": self.balance_rub}, "req_balance", 200)

    async def estimate(self, payload: dict[str, Any]) -> VibeResponse:
        self.estimate_calls.append(dict(payload))
        media_type = payload["type"]
        model = str(payload["model"])
        if model in self.invalid_estimate_models:
            return VibeResponse(
                {
                    "valid": False,
                    "warnings": ["provider validation rejected this request"],
                    "rejected": [],
                    "validation": {"media": [], "required_missing": []},
                },
                f"req_estimate_{len(self.estimate_calls)}",
                200,
            )
        prompt_limit = self.prompt_limits.get(model)
        prompt = str(payload.get("prompt", ""))
        if prompt_limit is not None and len(prompt) > prompt_limit:
            return VibeResponse(
                {
                    "valid": False,
                    "warnings": [
                        f"Prompt is too long: {len(prompt)} characters, model "
                        f'"{model}" accepts at most {prompt_limit}. '
                        "/generate would reject this request (nothing charged)."
                    ],
                    "rejected": [],
                    "validation": {"media": [], "required_missing": []},
                },
                f"req_estimate_{len(self.estimate_calls)}",
                200,
            )
        override = self.estimate_overrides.get(model)
        if media_type == "text":
            reserve = override or (4 if model == "claude-opus-5" else 5)
            data = {"valid": True, "dry_run": True, "reserve_rub": reserve}
        elif media_type == "image":
            catalog_price = float(CATALOG["models"]["image"][model]["price"])
            data = {
                "valid": True,
                "dry_run": True,
                "estimated_cost_rub": override or catalog_price,
            }
        else:
            data = {
                "valid": True,
                "dry_run": True,
                "estimated_cost_rub": override or 18,
            }
        return VibeResponse(data, f"req_estimate_{len(self.estimate_calls)}", 200)

    async def generate(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> VibeResponse:
        self.generate_calls.append((dict(payload), idempotency_key))
        if payload["model"] == self.fail_on_model:
            raise VibeAPIError(
                status_code=502,
                code="generation_failed",
                message="provider rejected the generation",
                request_id="req_failed_generation",
            )
        self._generation_counter += 1
        generation_id = self._generation_counter
        if payload["type"] == "text":
            cost = 2.1 if payload["model"] == "claude-opus-5" else 2.2
            text = f"result from {payload['model']}"
            if payload["model"] == "gpt-5.6-sol":
                text = self.selection_text_override or json.dumps(
                    {
                        "winner": "concept-1",
                        "rubric": [
                            {
                                "concept": "concept-1",
                                "clarity": 9,
                                "audience_fit": 8,
                                "distinctiveness": 7,
                                "claims_safety": 10,
                                "total": 34,
                                "reasons": ["clear offer"],
                            }
                        ],
                        "risks": [],
                        "banner_prompt": "safe winning banner prompt",
                    }
                )
            self.balance_rub = round(self.balance_rub - cost, 2)
            self._generation_costs[generation_id] = cost
            self._generation_types[generation_id] = "text"
            return VibeResponse(
                {
                    "status": "complete",
                    "generation_id": generation_id,
                    "model": payload["model"],
                    "text": text,
                    "cost": cost,
                    "reserved": 5,
                    "refunded": 2.8,
                    "balance_after": self.balance_rub,
                },
                f"req_generate_{generation_id}",
                200,
            )
        cost = (
            float(CATALOG["models"]["image"][payload["model"]]["price"])
            if payload["type"] == "image"
            else 18
        )
        self.balance_rub = round(self.balance_rub - cost, 2)
        self._generation_costs[generation_id] = cost
        self._generation_types[generation_id] = str(payload["type"])
        return VibeResponse(
            {
                "status": "processing",
                "generation_id": generation_id,
                "task_id": f"task_{generation_id}",
                "cost": cost,
                "balance_after": self.balance_rub,
            },
            f"req_generate_{generation_id}",
            200,
        )

    async def generation_status(self, generation_id: int | str) -> VibeResponse:
        numeric_id = int(generation_id)
        cost = self._generation_costs[numeric_id]
        return VibeResponse(
            {
                "status": "complete",
                "generation_id": numeric_id,
                "task_id": f"task_{numeric_id}",
                "cost": cost,
                "display_url": f"https://files.example/{numeric_id}",
                "balance_after": self.balance_rub,
            },
            f"req_status_{numeric_id}",
            200,
        )

    async def webhook_test(self, callback_url: str) -> VibeResponse:
        return VibeResponse(
            {
                "delivered": True,
                "http_status": 200,
                "callback_url": callback_url,
                "secret_formula": "test webhook secret",
            },
            "req_webhook_test",
            200,
        )


@pytest.fixture(autouse=True)
def configured_app() -> FakeVibeClient:
    fake = FakeVibeClient()
    store = StateStore("sqlite:///:memory:")
    app.state.vibe_client = fake
    app.state.state_store = store
    app.state.receipt_signer = ReceiptSigner()
    app.state.settings = Settings(
        vibe_api_base="https://lk.vibemarketolog.ru/api/agent",
        vibe_api_token="test-token",
        live_control_key="test-live-key",
        cors_origins=("*",),
        request_timeout_seconds=10,
        retry_attempts=1,
        public_base_url="https://vibepilot.example",
        vibe_webhook_secret="test-webhook-secret",
    )
    store.clear()
    yield fake
    store.clear()
    del app.state.vibe_client
    del app.state.state_store
    del app.state.receipt_signer
    del app.state.settings


client = TestClient(app)
LIVE_HEADERS = {"X-VibePilot-Live-Key": "test-live-key"}


def campaign(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "brief": "Кофейня в Казани: кампания для новых утренних гостей.",
        "budget_rub": 250,
        "priority": "balanced",
    }
    body.update(overrides)
    return body


def test_health_never_claims_automatic_live_mode() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["default_execution_mode"] == "demo"
    assert response.json()["live_mode_is_explicit"] is True


def test_demo_uses_real_catalog_names_but_never_claims_spend() -> None:
    planned = client.post("/api/v1/workflows/plan", json=campaign()).json()
    assert planned["mode"] == "demo"
    generation_models = [
        step["model"] for step in planned["steps"] if step["kind"] == "generation"
    ]
    assert generation_models == [
        "claude-opus-5",
        "gpt-5.6-sol",
        "nano-banana-2-lite",
        "pixverse-v6",
    ]

    executed = client.post(f"/api/v1/workflows/{planned['id']}/execute").json()
    assert executed["status"] == "awaiting_approval"
    assert executed["actual_spend_rub"] == 0
    assert executed["remaining_budget_rub"] == 250
    assert executed["steps"][0]["status"] == "simulated"
    assert executed["steps"][-1]["status"] == "awaiting_approval"

    receipt = client.get(f"/api/v1/workflows/{planned['id']}/receipt").json()
    assert receipt["verification"] == "simulation"
    assert receipt["budget"]["actual_charged_rub"] == 0
    assert all(step["generation_id"] is None for step in receipt["steps"])


def test_live_plan_refuses_to_exist_without_token() -> None:
    app.state.settings = Settings(
        vibe_api_base="https://lk.vibemarketolog.ru/api/agent",
        vibe_api_token="",
        live_control_key="test-live-key",
        cors_origins=("*",),
        request_timeout_seconds=10,
        retry_attempts=1,
    )
    response = client.post(
        "/api/v1/workflows/plan",
        json=campaign(execution_mode="live"),
        headers=LIVE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "live_mode_not_configured"
    assert app.state.state_store.workflow_count() == 0


def test_public_caller_cannot_spend_configured_token() -> None:
    response = client.post(
        "/api/v1/workflows/plan",
        json=campaign(execution_mode="live"),
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "live_control_key_required"
    assert app.state.state_store.workflow_count() == 0


def test_live_run_requires_real_responses_and_produces_receipt(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(execution_mode="live", approval_required_above_rub=10),
        headers=LIVE_HEADERS,
    ).json()
    assert planned["steps"][0]["pricing_source"] == "vibe_estimate"
    assert planned["steps"][2]["estimated_cost_rub"] == 9
    assert planned["steps"][-1]["estimated_cost_rub"] == 18

    running_image = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    ).json()
    assert running_image["status"] == "running"
    assert running_image["steps"][0]["status"] == "complete"
    assert running_image["steps"][1]["status"] == "complete"
    assert running_image["steps"][2]["status"] == "running"
    assert running_image["steps"][2]["generation_id"] == 103

    awaiting_video = client.post(
        f"/api/v1/workflows/{planned['id']}/refresh", headers=LIVE_HEADERS
    ).json()
    assert awaiting_video["status"] == "awaiting_approval"
    assert awaiting_video["steps"][3]["status"] == "complete"
    assert awaiting_video["steps"][4]["status"] == "awaiting_approval"
    assert awaiting_video["steps"][4]["requires_approval"] is True

    running_video = client.post(
        f"/api/v1/workflows/{planned['id']}/approve",
        json={"approved": True, "note": "Approved after fresh estimate"},
        headers=LIVE_HEADERS,
    ).json()
    assert running_video["status"] == "running"
    assert running_video["steps"][4]["generation_id"] == 104

    completed = client.post(
        f"/api/v1/workflows/{planned['id']}/refresh", headers=LIVE_HEADERS
    ).json()
    assert completed["status"] == "complete"
    assert completed["execution_verified"] is True
    assert completed["actual_spend_rub"] == 31.3
    assert completed["remaining_budget_rub"] == 218.7
    assert completed["reconciliation_status"] == "matched"
    assert completed["reconciled_balance_delta_rub"] == 31.3
    assert completed["reconciliation_difference_rub"] == 0

    generated_models = [call[0]["model"] for call in configured_app.generate_calls]
    assert generated_models == [
        "claude-opus-5",
        "gpt-5.6-sol",
        "nano-banana-2-lite",
        "pixverse-v6",
    ]
    assert "result from claude-opus-5" in configured_app.generate_calls[1][0]["prompt"]
    assert configured_app.generate_calls[-1][0]["pv_mode"] == "image"
    assert configured_app.generate_calls[-1][0]["image_urls"] == [
        "https://files.example/103"
    ]
    keys = [call[1] for call in configured_app.generate_calls]
    assert len(keys) == len(set(keys))

    receipt = client.get(
        f"/api/v1/workflows/{planned['id']}/receipt", headers=LIVE_HEADERS
    ).json()
    assert receipt["verification"] == "verified"
    assert receipt["steps"][0]["generation_id"] == 101
    assert receipt["steps"][-1]["generation_id"] == 104
    assert len(receipt["integrity"]["digest"]) == 64


def test_approval_threshold_is_a_real_policy() -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(approval_required_above_rub=50),
    ).json()
    executed = client.post(f"/api/v1/workflows/{planned['id']}/execute").json()
    assert executed["status"] == "complete"
    assert executed["steps"][-1]["requires_approval"] is False


def test_runtime_price_spike_uses_cheaper_catalog_fallback_before_spend(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(
            budget_rub=30,
            include_video=False,
            execution_mode="live",
            approval_required_above_rub=50,
        ),
        headers=LIVE_HEADERS,
    ).json()
    assert planned["steps"][2]["model"] == "nano-banana-2-lite"
    assert planned["steps"][2]["estimated_cost_rub"] == 9

    configured_app.estimate_overrides["nano-banana-2-lite"] = 40
    running = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    ).json()
    banner = running["steps"][2]
    assert banner["status"] == "running"
    assert banner["model"] == "z-image"
    assert banner["estimated_cost_rub"] == 1.2
    assert banner["applied_fallback"] == "nano-banana-2-lite -> z-image"
    assert configured_app.generate_calls[-1][0]["model"] == "z-image"

    completed = client.post(
        f"/api/v1/workflows/{planned['id']}/refresh", headers=LIVE_HEADERS
    ).json()
    assert completed["status"] == "complete"
    assert completed["actual_spend_rub"] == 5.5
    assert completed["reconciliation_status"] == "matched"
    assert any(
        event["event"] == "fallback_applied_before_spend"
        for event in completed["audit"]
    )


def test_runtime_prompt_limit_compacts_truncated_critic_output_before_spend(
    configured_app: FakeVibeClient,
) -> None:
    configured_app.prompt_limits["z-image"] = 1000
    configured_app.selection_text_override = (
        '{"winner":"Safe morning concept","rubric":"' + "x" * 1800
    )
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(
            budget_rub=20,
            priority="economy",
            include_video=False,
            execution_mode="live",
            approval_required_above_rub=50,
        ),
        headers=LIVE_HEADERS,
    ).json()

    running = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    ).json()
    banner = running["steps"][2]
    submitted_prompt = configured_app.generate_calls[-1][0]["prompt"]

    assert running["status"] == "running"
    assert banner["status"] == "running"
    assert banner["model"] == "z-image"
    assert len(submitted_prompt) <= 1000
    assert "Safe morning concept" in submitted_prompt
    assert banner["applied_fallback"].startswith(
        "prompt compacted after upstream validation"
    )
    assert any(
        event["event"] == "prompt_compacted_after_validation"
        for event in running["audit"]
    )


def test_runtime_validation_failure_returns_422_instead_of_500(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(
            include_video=False,
            execution_mode="live",
            approval_required_above_rub=50,
        ),
        headers=LIVE_HEADERS,
    ).json()
    configured_app.invalid_estimate_models.add("claude-opus-5")

    response = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "runtime_validation_rejected"
    assert response.json()["detail"]["failed_action_charged_rub"] == 0
    saved = client.get(
        f"/api/v1/workflows/{planned['id']}", headers=LIVE_HEADERS
    ).json()
    assert saved["status"] == "error"
    assert saved["actual_spend_rub"] == 0


def test_price_drift_policy_requires_approval_even_below_absolute_threshold(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(
            priority="quality",
            include_video=False,
            execution_mode="live",
            approval_required_above_rub=100,
            price_drift_tolerance_rub=2,
        ),
        headers=LIVE_HEADERS,
    ).json()
    assert planned["steps"][2]["estimated_cost_rub"] == 40

    configured_app.estimate_overrides["seedream-5-pro"] = 45
    awaiting = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    ).json()
    banner = awaiting["steps"][2]
    assert awaiting["status"] == "awaiting_approval"
    assert banner["estimated_cost_rub"] == 45
    assert banner["estimate_drift_rub"] == 5
    assert banner["requires_approval"] is True
    assert "Цена выросла" in banner["approval_reason"]
    assert all(call[0]["type"] == "text" for call in configured_app.generate_calls)


def test_approval_is_invalidated_when_price_changes_before_generate(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(execution_mode="live", approval_required_above_rub=10),
        headers=LIVE_HEADERS,
    ).json()
    client.post(f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS)
    awaiting = client.post(
        f"/api/v1/workflows/{planned['id']}/refresh", headers=LIVE_HEADERS
    ).json()
    assert awaiting["steps"][-1]["estimated_cost_rub"] == 18

    configured_app.estimate_overrides["pixverse-v6"] = 25
    still_awaiting = client.post(
        f"/api/v1/workflows/{planned['id']}/approve",
        json={"approved": True, "note": "Approved the 18-ruble estimate"},
        headers=LIVE_HEADERS,
    ).json()
    video = still_awaiting["steps"][-1]
    assert still_awaiting["status"] == "awaiting_approval"
    assert video["estimated_cost_rub"] == 25
    assert video["approved_at"] is None
    assert all(call[0]["type"] != "video" for call in configured_app.generate_calls)
    assert any(
        event["event"] == "approval_invalidated_before_spend"
        for event in still_awaiting["audit"]
    )


def test_video_is_removed_before_spend_when_runtime_price_exceeds_envelope(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(
            budget_rub=45,
            execution_mode="live",
            approval_required_above_rub=100,
        ),
        headers=LIVE_HEADERS,
    ).json()
    configured_app.estimate_overrides["pixverse-v6"] = 30

    running = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    ).json()
    assert running["steps"][2]["status"] == "running"

    completed = client.post(
        f"/api/v1/workflows/{planned['id']}/refresh", headers=LIVE_HEADERS
    ).json()
    video = completed["steps"][-1]
    assert completed["status"] == "complete"
    assert video["status"] == "skipped"
    assert video["actual_cost_rub"] == 0
    assert video["applied_fallback"] == "pixverse-v6 -> skipped"
    assert completed["actual_spend_rub"] == 13.3
    assert all(call[0]["type"] != "video" for call in configured_app.generate_calls)
    assert any(
        event["event"] == "scope_reduced_before_spend" for event in completed["audit"]
    )


def test_failed_generation_is_not_reported_as_complete_or_charged(
    configured_app: FakeVibeClient,
) -> None:
    configured_app.fail_on_model = "claude-opus-5"
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(execution_mode="live"),
        headers=LIVE_HEADERS,
    ).json()
    response = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    )
    assert response.status_code == 502

    saved = client.get(
        f"/api/v1/workflows/{planned['id']}", headers=LIVE_HEADERS
    ).json()
    assert saved["status"] == "error"
    assert saved["steps"][0]["status"] == "error"
    assert saved["steps"][0]["actual_cost_rub"] == 0
    assert saved["actual_spend_rub"] == 0
    assert saved["remaining_budget_rub"] == 250
    assert saved["execution_verified"] is False
    assert saved["audit"][-1]["details"]["charged_rub"] == 0


def test_same_receipt_state_has_stable_integrity_hash() -> None:
    planned = client.post("/api/v1/workflows/plan", json=campaign()).json()
    first = client.get(f"/api/v1/workflows/{planned['id']}/receipt").json()
    second = client.get(f"/api/v1/workflows/{planned['id']}/receipt").json()
    assert first["integrity"]["digest"] == second["integrity"]["digest"]


def test_receipt_has_independently_verifiable_ed25519_signature() -> None:
    planned = client.post("/api/v1/workflows/plan", json=campaign()).json()
    receipt = client.get(f"/api/v1/workflows/{planned['id']}/receipt").json()
    verified = client.post("/api/v1/receipts/verify", json=receipt).json()
    assert verified["valid"] is True
    assert receipt["signature"]["algorithm"] == "Ed25519"

    receipt["budget"]["actual_charged_rub"] = 999
    tampered = client.post("/api/v1/receipts/verify", json=receipt).json()
    assert tampered["valid"] is False


def test_server_rejects_valid_signature_from_unpinned_signer() -> None:
    planned = client.post("/api/v1/workflows/plan", json=campaign()).json()
    receipt = client.get(f"/api/v1/workflows/{planned['id']}/receipt").json()
    unsigned = deepcopy(receipt)
    unsigned.pop("signature")
    forged = deepcopy(unsigned)
    forged["signature"] = ReceiptSigner().sign(unsigned)

    response = client.post("/api/v1/receipts/verify", json=forged).json()
    assert response["valid"] is False
    assert response["identity_pinned"] is True
    assert "unexpected key id" in response["message"]


def test_counterfactual_planner_changes_scope_without_fake_quality_score() -> None:
    response = client.post(
        "/api/v1/budget/compare",
        json={
            "brief": campaign()["brief"],
            "budgets_rub": [15, 30, 60, 100],
            "reserve_rub": 5,
        },
    )
    assert response.status_code == 200
    comparison = response.json()
    scenarios = comparison["scenarios"]
    assert [(item["budget_rub"], item["priority"]) for item in scenarios] == [
        (15, "economy"),
        (30, "balanced"),
        (60, "economy"),
        (100, "quality"),
    ]
    assert scenarios[0]["deliverables"] == ["banner"]
    assert scenarios[2]["deliverables"] == ["banner", "video"]
    assert "Никакого выдуманного quality score" in comparison["methodology"][0]


def test_budget_contract_is_immutable_input_to_workflow() -> None:
    compiled = client.post(
        "/api/v1/contracts/compile",
        json={
            "brief": campaign()["brief"],
            "budgets_rub": [60],
            "reserve_rub": 5,
        },
    )
    assert compiled.status_code == 200
    contract = compiled.json()
    assert contract["status"] == "draft"
    assert contract["selected_scenario"]["feasible"] is True
    assert len(contract["digest"]) == 64

    activated = client.post(f"/api/v1/contracts/{contract['id']}/activate").json()
    assert activated["contract_id"] == contract["id"]
    assert activated["contract_digest"] == contract["digest"]
    assert activated["reserve_rub"] == 5

    stored = client.get(f"/api/v1/contracts/{contract['id']}").json()
    assert stored["status"] == "activated"
    assert stored["workflow_id"] == activated["id"]


def test_tampered_budget_contract_is_rejected_before_activation() -> None:
    contract = client.post(
        "/api/v1/contracts/compile",
        json={
            "brief": campaign()["brief"],
            "budgets_rub": [60],
            "reserve_rub": 5,
        },
    ).json()
    stored = app.state.state_store.get_contract(contract["id"])
    assert stored is not None
    stored.policy.max_total_rub = 500
    app.state.state_store.save_contract(stored)

    response = client.post(f"/api/v1/contracts/{contract['id']}/activate")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "budget_contract_integrity_failed"
    assert response.json()["detail"]["charged_rub"] == 0


def test_signed_webhook_completes_async_step_and_continues(
    configured_app: FakeVibeClient,
) -> None:
    planned = client.post(
        "/api/v1/workflows/plan",
        json=campaign(execution_mode="live"),
        headers=LIVE_HEADERS,
    ).json()
    running = client.post(
        f"/api/v1/workflows/{planned['id']}/execute", headers=LIVE_HEADERS
    ).json()
    generation_id = running["steps"][2]["generation_id"]
    payload = {
        "event": "generation.complete",
        "generation_id": generation_id,
        "task_id": f"task_{generation_id}",
        "status": "complete",
        "cost": 9,
        "display_url": f"https://files.example/{generation_id}",
        "refunded": False,
        "attempt": 1,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    applied = client.post(
        "/api/v1/webhooks/vibe",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Signature": signature,
            "X-Vibe-Event": "generation.complete",
        },
    )
    assert applied.status_code == 200
    assert applied.json()["workflow_status"] == "awaiting_approval"

    saved = client.get(
        f"/api/v1/workflows/{planned['id']}", headers=LIVE_HEADERS
    ).json()
    assert saved["steps"][2]["status"] == "complete"
    assert saved["steps"][3]["status"] == "complete"
    assert saved["steps"][4]["status"] == "awaiting_approval"


def test_webhook_rejects_invalid_signature() -> None:
    response = client.post(
        "/api/v1/webhooks/vibe",
        json={"generation_id": 123, "status": "complete"},
        headers={"X-Vibe-Signature": "bad"},
    )
    assert response.status_code == 401


def test_signed_webhook_self_test_is_accepted_without_generation_id() -> None:
    payload = {"event": "webhook.test", "attempt": 1}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/api/v1/webhooks/vibe",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Signature": signature,
            "X-Vibe-Event": "webhook.test",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "verified", "event": "webhook.test"}


def test_operator_can_trigger_free_end_to_end_webhook_delivery_test() -> None:
    response = client.post(
        "/api/v1/integrations/vibe/webhook-test", headers=LIVE_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["delivered"] is True
    assert response.json()["request_id"] == "req_webhook_test"


def test_sql_store_persists_and_detects_stale_revision(tmp_path: Any) -> None:
    planned = client.post("/api/v1/workflows/plan", json=campaign()).json()
    workflow = Workflow.model_validate(planned).model_copy(update={"revision": 0})
    database = tmp_path / "state.db"
    first_store = StateStore(f"sqlite:///{database}")
    first_store.save_workflow(workflow)

    second_store = StateStore(f"sqlite:///{database}")
    first_copy = first_store.get_workflow(workflow.id)
    stale_copy = second_store.get_workflow(workflow.id)
    assert first_copy is not None and stale_copy is not None
    first_copy.status = "running"
    first_store.save_workflow(first_copy)
    stale_copy.status = "cancelled"
    with pytest.raises(StoreConflict):
        second_store.save_workflow(stale_copy)
