from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ExecutionMode = Literal["demo", "live"]
StepStatus = Literal[
    "planned",
    "estimating",
    "running",
    "complete",
    "simulated",
    "awaiting_approval",
    "skipped",
    "blocked",
    "error",
]
WorkflowStatus = Literal[
    "planned",
    "running",
    "awaiting_approval",
    "complete",
    "cancelled",
    "blocked",
    "error",
]
ReconciliationStatus = Literal["not_run", "matched", "mismatch", "unavailable"]


class CampaignRequest(BaseModel):
    brief: str = Field(min_length=20, max_length=4_000)
    budget_rub: float = Field(default=250, ge=10, le=50_000)
    priority: Literal["economy", "balanced", "quality"] = "balanced"
    execution_mode: ExecutionMode = "demo"
    approval_required_above_rub: float = Field(default=10, ge=1, le=50_000)
    include_video: bool = True
    reserve_rub: float = Field(default=5, ge=0, le=10_000)
    price_drift_tolerance_rub: float = Field(default=2, ge=0, le=10_000)


class ApprovalRequest(BaseModel):
    approved: bool = True
    note: str | None = Field(default=None, max_length=500)


class AuditEvent(BaseModel):
    at: datetime
    event: str
    step_id: str | None = None
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    id: str
    title: str
    purpose: str
    kind: Literal["generation", "guard"] = "generation"
    media_type: Literal["text", "image", "video", "guard"]
    model: str
    cost_rub: float = 0
    estimated_cost_rub: float = 0
    initial_estimated_cost_rub: float = 0
    estimate_drift_rub: float = 0
    actual_cost_rub: float = 0
    refunded_rub: float = 0
    pricing_source: Literal[
        "vibe_capabilities", "vibe_estimate", "runtime_estimate", "free"
    ]
    duration_hint: str
    status: StepStatus = "planned"
    reason: str
    requires_approval: bool = False
    approval_reason: str | None = None
    approved_at: datetime | None = None
    approval_note: str | None = None
    approved_estimate_rub: float | None = None
    approved_request_fingerprint: str | None = None
    request_payload: dict[str, Any] = Field(default_factory=dict)
    fallback_payloads: list[dict[str, Any]] = Field(default_factory=list)
    applied_fallback: str | None = None
    request_fingerprint: str | None = None
    idempotency_key: str | None = None
    estimate_snapshot: dict[str, Any] | None = None
    generation_id: int | str | None = None
    task_id: str | None = None
    upstream_request_id: str | None = None
    result_url: str | None = None
    result_urls: list[str] = Field(default_factory=list)
    text_result: str | None = None
    structured_result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    simulated: bool = False


class Workflow(BaseModel):
    id: str
    brief: str
    budget_rub: float
    priority: str
    status: WorkflowStatus
    required_cost_rub: float
    maximum_cost_rub: float
    remaining_budget_rub: float
    projected_remaining_budget_rub: float
    actual_spend_rub: float = 0
    refunded_rub: float = 0
    approval_required_above_rub: float
    reserve_rub: float = 0
    price_drift_tolerance_rub: float = 0
    include_video: bool
    steps: list[Step]
    created_at: datetime
    updated_at: datetime
    mode: ExecutionMode
    trace_id: str
    revision: int = 0
    contract_id: str | None = None
    contract_digest: str | None = None
    catalog_digest: str | None = None
    webhook_enabled: bool = False
    execution_verified: bool = False
    balance_before_rub: float | None = None
    balance_after_rub: float | None = None
    reconciliation_status: ReconciliationStatus = "not_run"
    reconciled_at: datetime | None = None
    reconciled_balance_delta_rub: float | None = None
    reconciliation_difference_rub: float | None = None
    adaptations: list[str] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)


class BudgetComparisonRequest(BaseModel):
    brief: str = Field(min_length=20, max_length=4_000)
    budgets_rub: list[float] = Field(
        default_factory=lambda: [15, 30, 60, 100], min_length=1, max_length=8
    )
    execution_mode: ExecutionMode = "demo"
    approval_required_above_rub: float = Field(default=10, ge=1, le=50_000)
    reserve_rub: float = Field(default=5, ge=0, le=10_000)
    price_drift_tolerance_rub: float = Field(default=2, ge=0, le=10_000)
    include_video: bool = True


class ContractPolicy(BaseModel):
    max_total_rub: float
    approval_above_rub: float
    reserve_rub: float
    price_drift_tolerance_rub: float
    fallback_strategy: Literal["degrade_before_stop"] = "degrade_before_stop"


class ScenarioStep(BaseModel):
    id: str
    media_type: str
    model: str
    estimated_cost_rub: float
    requires_approval: bool


class BudgetScenario(BaseModel):
    id: str
    budget_rub: float
    priority: Literal["economy", "balanced", "quality"]
    include_video: bool
    deliverables: list[str]
    required_cost_rub: float
    maximum_cost_rub: float
    reserve_rub: float
    total_envelope_rub: float
    feasible: bool
    pricing_basis: Literal["catalog_indicative", "vibe_estimate"]
    omitted: list[str] = Field(default_factory=list)
    reason: str
    steps: list[ScenarioStep]


class BudgetComparison(BaseModel):
    brief: str
    mode: ExecutionMode
    catalog_digest: str
    scenarios: list[BudgetScenario]
    generated_at: datetime
    methodology: list[str]


class BudgetContract(BaseModel):
    id: str
    brief: str
    mode: ExecutionMode
    status: Literal["draft", "activated"] = "draft"
    policy: ContractPolicy
    selected_scenario: BudgetScenario
    alternatives: list[BudgetScenario]
    steps: list[Step]
    catalog_digest: str
    digest: str
    created_at: datetime
    activated_at: datetime | None = None
    workflow_id: str | None = None
    revision: int = 0
