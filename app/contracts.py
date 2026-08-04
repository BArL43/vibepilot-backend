from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.models import (
    BudgetComparison,
    BudgetComparisonRequest,
    BudgetContract,
    BudgetScenario,
    CampaignRequest,
    ContractPolicy,
    ScenarioStep,
    Step,
)
from app.planner import build_steps

PriceSteps = Callable[[list[Step]], Awaitable[None]]
PRIORITY_RANK = {"economy": 1, "balanced": 2, "quality": 3}


def canonical_digest(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def catalog_digest(catalog: dict[str, Any]) -> str:
    relevant = {
        "models": catalog.get("models", {}),
        "text_models": catalog.get("text_models", {}),
    }
    return canonical_digest(relevant)


def budget_contract_digest(contract: BudgetContract) -> str:
    unsigned = {
        "id": contract.id,
        "brief": contract.brief,
        "mode": contract.mode,
        "policy": contract.policy.model_dump(mode="json"),
        "selected_scenario": contract.selected_scenario.model_dump(mode="json"),
        "steps": [step.model_dump(mode="json") for step in contract.steps],
        "catalog_digest": contract.catalog_digest,
        "created_at": contract.created_at.isoformat(),
    }
    return canonical_digest(unsigned)


def _costs(steps: list[Step]) -> tuple[float, float]:
    required = round(
        sum(
            step.estimated_cost_rub
            for step in steps
            if step.kind == "generation" and not step.requires_approval
        ),
        2,
    )
    maximum = round(
        sum(step.estimated_cost_rub for step in steps if step.kind == "generation"),
        2,
    )
    return required, maximum


def _deliverables(steps: list[Step]) -> list[str]:
    result: list[str] = []
    if any(step.media_type == "image" for step in steps):
        result.append("banner")
    if any(step.media_type == "video" for step in steps):
        result.append("video")
    return result


async def candidates_for_budget(
    *,
    catalog: dict[str, Any],
    request: BudgetComparisonRequest,
    budget_rub: float,
    price_steps: PriceSteps | None = None,
) -> list[tuple[BudgetScenario, list[Step]]]:
    candidates: list[tuple[BudgetScenario, list[Step]]] = []
    video_options = [False, True] if request.include_video else [False]
    for include_video in video_options:
        for priority in ("economy", "balanced", "quality"):
            campaign = CampaignRequest(
                brief=request.brief,
                budget_rub=max(10, budget_rub),
                priority=priority,
                execution_mode=request.execution_mode,
                approval_required_above_rub=request.approval_required_above_rub,
                include_video=include_video,
                reserve_rub=request.reserve_rub,
                price_drift_tolerance_rub=request.price_drift_tolerance_rub,
            )
            steps = build_steps(catalog, campaign)
            if price_steps is not None:
                await price_steps(steps)
            required, maximum = _costs(steps)
            total = round(maximum + request.reserve_rub, 2)
            feasible = total <= budget_rub
            deliverables = _deliverables(steps)
            omitted = [] if include_video else ["video"]
            reason = (
                f"Вмещает {', '.join(deliverables)} и резерв "
                f"{request.reserve_rub:.2f} ₽ без превышения конверта."
                if feasible
                else f"Требует ещё {total - budget_rub:.2f} ₽ до безопасного потолка."
            )
            scenario_payload = {
                "budget": budget_rub,
                "priority": priority,
                "video": include_video,
                "models": [step.model for step in steps],
                "maximum": maximum,
            }
            scenario = BudgetScenario(
                id=f"scn_{canonical_digest(scenario_payload)[:12]}",
                budget_rub=budget_rub,
                priority=priority,
                include_video=include_video,
                deliverables=deliverables,
                required_cost_rub=required,
                maximum_cost_rub=maximum,
                reserve_rub=request.reserve_rub,
                total_envelope_rub=total,
                feasible=feasible,
                pricing_basis=(
                    "vibe_estimate"
                    if request.execution_mode == "live"
                    else "catalog_indicative"
                ),
                omitted=omitted,
                reason=reason,
                steps=[
                    ScenarioStep(
                        id=step.id,
                        media_type=step.media_type,
                        model=step.model,
                        estimated_cost_rub=step.estimated_cost_rub,
                        requires_approval=step.requires_approval,
                    )
                    for step in steps
                    if step.kind == "generation"
                ],
            )
            candidates.append((scenario, steps))
    return candidates


def select_scenario(
    candidates: list[tuple[BudgetScenario, list[Step]]],
) -> tuple[BudgetScenario, list[Step]]:
    feasible = [candidate for candidate in candidates if candidate[0].feasible]
    if feasible:
        return max(
            feasible,
            key=lambda candidate: (
                len(candidate[0].deliverables),
                PRIORITY_RANK[candidate[0].priority],
                -candidate[0].total_envelope_rub,
            ),
        )
    return min(candidates, key=lambda candidate: candidate[0].total_envelope_rub)


async def build_comparison(
    *,
    catalog: dict[str, Any],
    request: BudgetComparisonRequest,
    price_steps: PriceSteps | None = None,
) -> BudgetComparison:
    scenarios: list[BudgetScenario] = []
    for budget in sorted(set(round(value, 2) for value in request.budgets_rub)):
        candidates = await candidates_for_budget(
            catalog=catalog,
            request=request,
            budget_rub=budget,
            price_steps=price_steps,
        )
        selected, _ = select_scenario(candidates)
        scenarios.append(selected)
    return BudgetComparison(
        brief=request.brief,
        mode=request.execution_mode,
        catalog_digest=catalog_digest(catalog),
        scenarios=scenarios,
        generated_at=datetime.now(UTC),
        methodology=[
            "Никакого выдуманного quality score: сравниваются состав результата, "
            "выбранный пользователем tier и проверяемая цена.",
            "Сначала максимизируется число deliverables, затем tier качества, "
            "затем минимизируется расход внутри одинакового результата.",
            "Резерв не расходуется: он остаётся страховкой от токенов и дрейфа цены.",
        ],
    )


async def build_contract(
    *,
    catalog: dict[str, Any],
    request: BudgetComparisonRequest,
    price_steps: PriceSteps | None = None,
) -> BudgetContract:
    if len(request.budgets_rub) != 1:
        raise ValueError("A Budget Contract requires exactly one budget")
    budget = round(request.budgets_rub[0], 2)
    candidates = await candidates_for_budget(
        catalog=catalog,
        request=request,
        budget_rub=budget,
        price_steps=price_steps,
    )
    selected, selected_steps = select_scenario(candidates)
    if not selected.feasible:
        raise ValueError(
            f"Safe minimum is {selected.total_envelope_rub:.2f} ₽ for this brief"
        )
    now = datetime.now(UTC)
    contract_id = f"ctr_{uuid.uuid4().hex[:12]}"
    contract = BudgetContract(
        id=contract_id,
        brief=request.brief,
        mode=request.execution_mode,
        policy=ContractPolicy(
            max_total_rub=budget,
            approval_above_rub=request.approval_required_above_rub,
            reserve_rub=request.reserve_rub,
            price_drift_tolerance_rub=request.price_drift_tolerance_rub,
        ),
        selected_scenario=selected,
        alternatives=[
            scenario for scenario, _ in candidates if scenario.id != selected.id
        ],
        steps=selected_steps,
        catalog_digest=catalog_digest(catalog),
        digest="",
        created_at=now,
    )
    contract.digest = budget_contract_digest(contract)
    return contract
