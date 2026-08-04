from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from asyncio import Lock
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings
from app.contracts import (
    budget_contract_digest,
    build_comparison,
    build_contract,
    catalog_digest,
)
from app.models import (
    ApprovalRequest,
    AuditEvent,
    BudgetComparison,
    BudgetComparisonRequest,
    BudgetContract,
    CampaignRequest,
    Step,
    Workflow,
)
from app.planner import (
    CatalogError,
    apply_approval_policy,
    build_steps,
    estimate_cost_from_response,
)
from app.receipts import ReceiptSigner, verify_signed_receipt
from app.store import StateStore, StoreConflict
from app.vibe_client import VibeAPIError, VibeClient, VibeResponse

API_VERSION = "0.3.0"
SETTINGS = Settings.from_env()
STATE_STORE = StateStore(SETTINGS.database_url)
RECEIPT_SIGNER = ReceiptSigner(SETTINGS.receipt_signing_key)
WORKFLOW_LOCKS: dict[str, Lock] = {}

app = FastAPI(
    title="VibePilot API",
    description=(
        "Budget-aware, receipt-producing orchestration for the live "
        "VibeMarketolog Agent API."
    ),
    version=API_VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(SETTINGS.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _money(value: Any) -> float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    return 0


def _settings() -> Settings:
    return getattr(app.state, "settings", SETTINGS)


def _store() -> StateStore:
    return getattr(app.state, "state_store", STATE_STORE)


def _signer() -> ReceiptSigner:
    return getattr(app.state, "receipt_signer", RECEIPT_SIGNER)


def _workflow_lock(workflow_id: str) -> Lock:
    return WORKFLOW_LOCKS.setdefault(workflow_id, Lock())


def _locked_workflow(function: Any) -> Any:
    @wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        workflow_id = kwargs.get("workflow_id")
        if not isinstance(workflow_id, str):
            raise RuntimeError("locked workflow endpoint has no workflow_id")
        async with _workflow_lock(workflow_id):
            return await function(*args, **kwargs)

    return wrapper


def _require_live_access(presented_key: str | None) -> None:
    settings = _settings()
    if not settings.vibe_api_token:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_mode_not_configured",
                "message": "VIBE_API_TOKEN is not configured.",
            },
        )
    if not settings.live_control_key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "live_control_not_configured",
                "message": (
                    "VIBEPILOT_LIVE_KEY must be configured before public live "
                    "execution is enabled."
                ),
            },
        )
    if not presented_key or not hmac.compare_digest(
        presented_key, settings.live_control_key
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "live_control_key_required",
                "message": "A valid X-VibePilot-Live-Key header is required.",
            },
        )


def _has_live_access(presented_key: str | None) -> bool:
    settings = _settings()
    return bool(
        presented_key
        and settings.live_control_key
        and hmac.compare_digest(presented_key, settings.live_control_key)
    )


@asynccontextmanager
async def _client() -> AsyncIterator[VibeClient]:
    override = getattr(app.state, "vibe_client", None)
    if override is not None:
        yield override
        return
    async with VibeClient(_settings()) as client:
        yield client


def _save(workflow: Workflow) -> None:
    workflow.updated_at = _utcnow()
    _store().save_workflow(workflow)


def _get(workflow_id: str) -> Workflow:
    workflow = _store().get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return workflow


@app.exception_handler(StoreConflict)
async def store_conflict_handler(_: object, exc: StoreConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "code": "state_revision_conflict",
                "message": str(exc),
                "retryable": True,
            }
        },
    )


def _audit(
    workflow: Workflow,
    event: str,
    *,
    step: Step | None = None,
    request_id: str | None = None,
    **details: Any,
) -> None:
    workflow.audit.append(
        AuditEvent(
            at=_utcnow(),
            event=event,
            step_id=step.id if step else None,
            request_id=request_id,
            details=details,
        )
    )


def _extract_balance(payload: dict[str, Any]) -> float | None:
    candidates: list[Any] = [
        payload.get("balance"),
        payload.get("current"),
        payload.get("balance_rub"),
        payload.get("rub_balance"),
    ]
    nested = payload.get("balance")
    if isinstance(nested, dict):
        candidates.extend(
            [nested.get("current"), nested.get("rub"), nested.get("balance")]
        )
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return round(float(candidate), 2)
    return None


def _recompute_costs(workflow: Workflow) -> None:
    workflow.actual_spend_rub = round(
        sum(step.actual_cost_rub for step in workflow.steps), 2
    )
    workflow.refunded_rub = round(sum(step.refunded_rub for step in workflow.steps), 2)
    net = workflow.actual_spend_rub - workflow.refunded_rub
    workflow.remaining_budget_rub = round(max(0, workflow.budget_rub - net), 2)
    projected = sum(
        step.estimated_cost_rub
        for step in workflow.steps
        if step.kind == "generation" and step.status not in {"skipped", "error"}
    )
    workflow.projected_remaining_budget_rub = round(workflow.budget_rub - projected, 2)


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key(workflow: Workflow, step: Step, fingerprint: str) -> str:
    return f"vp-{workflow.id}-{step.id}-{fingerprint[:16]}"


def _upstream_error_response(exc: VibeAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "detail": {
                "code": "vibe_upstream_error",
                "upstream_status": exc.status_code,
                "upstream_code": exc.code,
                "message": exc.message,
                "request_id": exc.request_id,
                "retry_after": exc.retry_after,
            }
        },
    )


@app.exception_handler(VibeAPIError)
async def vibe_error_handler(_: object, exc: VibeAPIError) -> JSONResponse:
    return _upstream_error_response(exc)


def _estimate_is_valid(data: dict[str, Any]) -> bool:
    return data.get("valid", True) is not False


async def _estimate_step(
    client: VibeClient,
    step: Step,
    *,
    pricing_source: str = "vibe_estimate",
) -> VibeResponse:
    response = await client.estimate(step.request_payload)
    if not _estimate_is_valid(response.data):
        raise CatalogError(
            "VibeMarketolog rejected the dry-run: "
            + json.dumps(
                {
                    "warnings": response.data.get("warnings", []),
                    "rejected": response.data.get("rejected", []),
                    "validation": response.data.get("validation", {}),
                },
                ensure_ascii=False,
            )
        )
    cost = estimate_cost_from_response(response.data)
    if pricing_source == "vibe_estimate" or step.initial_estimated_cost_rub == 0:
        step.initial_estimated_cost_rub = cost
    step.estimated_cost_rub = cost
    step.estimate_drift_rub = round(cost - step.initial_estimated_cost_rub, 2)
    step.cost_rub = cost
    step.pricing_source = pricing_source  # type: ignore[assignment]
    step.estimate_snapshot = response.data
    step.upstream_request_id = response.request_id
    return response


def _attach_callback(step: Step) -> None:
    settings = _settings()
    if (
        step.kind == "generation"
        and step.media_type != "text"
        and settings.public_base_url
        and settings.vibe_webhook_secret
    ):
        step.request_payload["callback_url"] = (
            f"{settings.public_base_url}/api/v1/webhooks/vibe"
        )


def _apply_runtime_approval_policy(workflow: Workflow, step: Step) -> None:
    apply_approval_policy(step, workflow.approval_required_above_rub)
    if step.estimate_drift_rub > workflow.price_drift_tolerance_rub:
        drift_reason = (
            f"Цена выросла на {step.estimate_drift_rub:.2f} ₽, что выше "
            f"допуска {workflow.price_drift_tolerance_rub:.2f} ₽."
        )
        step.requires_approval = True
        step.approval_reason = (
            f"{step.approval_reason} {drift_reason}"
            if step.approval_reason
            else drift_reason
        )


def _approval_is_current(workflow: Workflow, step: Step) -> bool:
    if step.approved_at is None:
        return False
    current_fingerprint = _fingerprint(step.request_payload)
    payload_changed = step.approved_request_fingerprint != current_fingerprint
    approved_estimate = step.approved_estimate_rub
    price_outgrew_approval = (
        approved_estimate is None
        or step.estimated_cost_rub - approved_estimate
        > workflow.price_drift_tolerance_rub
    )
    if not payload_changed and not price_outgrew_approval:
        return True

    _audit(
        workflow,
        "approval_invalidated_before_spend",
        step=step,
        payload_changed=payload_changed,
        approved_estimate_rub=approved_estimate,
        fresh_estimate_rub=step.estimated_cost_rub,
        tolerance_rub=workflow.price_drift_tolerance_rub,
    )
    step.approved_at = None
    step.approval_note = None
    step.approved_estimate_rub = None
    step.approved_request_fingerprint = None
    return False


async def _runtime_estimate_with_fallbacks(
    client: VibeClient, workflow: Workflow, step: Step
) -> VibeResponse | None:
    original_payload = deepcopy(step.request_payload)
    original_model = step.model
    primary_error: CatalogError | VibeAPIError | None = None
    try:
        response = await _estimate_step(client, step, pricing_source="runtime_estimate")
    except (CatalogError, VibeAPIError) as exc:
        response = None
        primary_error = exc
    available = max(0, workflow.remaining_budget_rub - workflow.reserve_rub)
    if response is not None and step.estimated_cost_rub <= available:
        return response

    primary_state = {
        "estimated_cost_rub": step.estimated_cost_rub,
        "estimate_drift_rub": step.estimate_drift_rub,
        "cost_rub": step.cost_rub,
        "estimate_snapshot": deepcopy(step.estimate_snapshot),
        "upstream_request_id": step.upstream_request_id,
    }
    can_skip = False
    for fallback in step.fallback_payloads:
        if fallback.get("_fallback_action") == "skip":
            can_skip = True
            continue
        step.request_payload = deepcopy(fallback)
        _prepare_dependent_payload(workflow, step)
        _attach_callback(step)
        try:
            fallback_response = await _estimate_step(
                client, step, pricing_source="runtime_estimate"
            )
        except (CatalogError, VibeAPIError):
            continue
        if step.estimated_cost_rub <= available:
            previous_model = str(original_payload.get("model", step.model))
            step.model = str(step.request_payload["model"])
            step.applied_fallback = f"{previous_model} -> {step.model}"
            _audit(
                workflow,
                "fallback_applied_before_spend",
                step=step,
                request_id=fallback_response.request_id,
                previous_model=previous_model,
                selected_model=step.model,
                estimate_rub=step.estimated_cost_rub,
            )
            return fallback_response
    step.request_payload = original_payload
    step.model = original_model
    for field, value in primary_state.items():
        setattr(step, field, value)

    if can_skip:
        step.status = "skipped"
        step.applied_fallback = f"{original_model} -> skipped"
        step.requires_approval = False
        step.approval_reason = (
            "Шаг исключён до списания: ни один вариант не помещается в "
            "оставшийся бюджетный конверт."
        )
        _audit(
            workflow,
            "scope_reduced_before_spend",
            step=step,
            request_id=response.request_id if response else None,
            omitted_model=original_model,
            available_rub=available,
            latest_estimate_rub=(
                step.estimated_cost_rub if response is not None else None
            ),
            reason=("over_budget" if response is not None else "estimate_rejected"),
        )
        return response

    if primary_error is not None:
        raise primary_error
    if response is None:
        raise RuntimeError("runtime estimate produced neither response nor error")
    return response


def _workflow_costs(steps: list[Step]) -> tuple[float, float]:
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


def _new_workflow(
    *,
    brief: str,
    budget_rub: float,
    priority: str,
    mode: str,
    approval_required_above_rub: float,
    reserve_rub: float,
    price_drift_tolerance_rub: float,
    include_video: bool,
    steps: list[Step],
    catalog_fingerprint: str,
    balance_before: float | None = None,
    adaptations: list[str] | None = None,
    contract: BudgetContract | None = None,
) -> Workflow:
    required, maximum = _workflow_costs(steps)
    now = _utcnow()
    return Workflow(
        id=f"wf_{uuid.uuid4().hex[:12]}",
        brief=brief,
        budget_rub=budget_rub,
        priority=priority,
        status="planned",
        required_cost_rub=required,
        maximum_cost_rub=maximum,
        remaining_budget_rub=budget_rub,
        projected_remaining_budget_rub=round(budget_rub - maximum - reserve_rub, 2),
        approval_required_above_rub=approval_required_above_rub,
        reserve_rub=reserve_rub,
        price_drift_tolerance_rub=price_drift_tolerance_rub,
        include_video=include_video,
        steps=deepcopy(steps),
        created_at=now,
        updated_at=now,
        mode=mode,  # type: ignore[arg-type]
        trace_id=uuid.uuid4().hex,
        contract_id=contract.id if contract else None,
        contract_digest=contract.digest if contract else None,
        catalog_digest=catalog_fingerprint,
        webhook_enabled=bool(
            _settings().public_base_url and _settings().vibe_webhook_secret
        ),
        balance_before_rub=balance_before,
        balance_after_rub=balance_before,
        adaptations=adaptations or [],
    )


async def _priced_steps(
    client: VibeClient,
    catalog: dict[str, Any],
    request: CampaignRequest,
) -> list[Step]:
    steps = build_steps(catalog, request)
    for step in steps:
        _attach_callback(step)
    if request.execution_mode == "live":
        for step in steps:
            if step.kind != "generation":
                continue
            await _estimate_step(client, step)
            apply_approval_policy(step, request.approval_required_above_rub)
    return steps


def _prepare_dependent_payload(workflow: Workflow, step: Step) -> None:
    if step.id == "selection":
        concepts = next(item for item in workflow.steps if item.id == "concepts")
        if concepts.text_result:
            base = step.request_payload["prompt"].split("\n\nКонцепции:", 1)[0]
            step.request_payload["prompt"] = (
                f"{base}\n\nКонцепции:\n{concepts.text_result}"
            )
    elif step.id == "banner":
        selection = next(item for item in workflow.steps if item.id == "selection")
        banner_prompt = None
        if selection.structured_result:
            candidate = selection.structured_result.get("banner_prompt")
            if isinstance(candidate, str):
                banner_prompt = candidate
        if banner_prompt or selection.text_result:
            base = step.request_payload["prompt"].split("\n\nРешение", 1)[0]
            step.request_payload["prompt"] = (
                f"{base}\n\nРешение креативного директора:\n"
                f"{(banner_prompt or selection.text_result or '')[:5000]}"
            )
    elif step.id == "video":
        banner = next(item for item in workflow.steps if item.id == "banner")
        if not banner.result_url:
            return
        model = step.model
        if model == "pixverse-v6":
            step.request_payload.update(
                {"pv_mode": "image", "image_urls": [banner.result_url]}
            )
            step.request_payload.pop("aspect_ratio", None)
        elif model == "seedance-2-mini":
            step.request_payload["first_frame_url"] = banner.result_url
        elif model.startswith("veo3"):
            step.request_payload.update(
                {
                    "image_urls": [banner.result_url],
                    "generation_type": "image-to-video",
                }
            )


def _record_generation_result(step: Step, response: VibeResponse) -> None:
    data = response.data
    step.upstream_request_id = response.request_id or step.upstream_request_id
    step.generation_id = data.get("generation_id") or step.generation_id
    step.task_id = data.get("task_id") or step.task_id
    step.actual_cost_rub = _money(data.get("cost") or step.actual_cost_rub)
    text = data.get("text") or data.get("text_content")
    if isinstance(text, str):
        step.text_result = text
        if step.id == "selection":
            candidate = text.strip()
            if candidate.startswith("```"):
                candidate = candidate.removeprefix("```json").removeprefix("```")
                candidate = candidate.removesuffix("```").strip()
            try:
                structured = json.loads(candidate)
            except json.JSONDecodeError:
                structured = None
            if isinstance(structured, dict):
                step.structured_result = structured
    display_url = data.get("display_url") or data.get("result_url")
    if isinstance(display_url, str):
        step.result_url = display_url
    result_urls = data.get("result_urls")
    if isinstance(result_urls, list):
        step.result_urls = [str(url) for url in result_urls]


def _sync_workflow_balance(workflow: Workflow, data: dict[str, Any]) -> None:
    after = data.get("balance_after")
    if isinstance(after, (int, float)) and not isinstance(after, bool):
        workflow.balance_after_rub = round(float(after), 2)


async def _reconcile_balance(workflow: Workflow, client: VibeClient) -> None:
    """Cross-check per-generation costs against the account balance delta."""
    try:
        response = await client.balance()
    except VibeAPIError as exc:
        workflow.reconciliation_status = "unavailable"
        workflow.reconciled_at = _utcnow()
        _audit(
            workflow,
            "balance_reconciliation_unavailable",
            request_id=exc.request_id,
            upstream_code=exc.code,
        )
        return

    balance_after = _extract_balance(response.data)
    workflow.balance_after_rub = balance_after
    workflow.reconciled_at = _utcnow()
    if workflow.balance_before_rub is None or balance_after is None:
        workflow.reconciliation_status = "unavailable"
        _audit(
            workflow,
            "balance_reconciliation_unavailable",
            request_id=response.request_id,
            reason="balance_field_missing",
        )
        return

    balance_delta = round(workflow.balance_before_rub - balance_after, 2)
    recorded_net_spend = round(workflow.actual_spend_rub - workflow.refunded_rub, 2)
    difference = round(balance_delta - recorded_net_spend, 2)
    workflow.reconciled_balance_delta_rub = balance_delta
    workflow.reconciliation_difference_rub = difference
    workflow.reconciliation_status = (
        "matched" if abs(difference) <= 0.01 else "mismatch"
    )
    _audit(
        workflow,
        "balance_reconciled",
        request_id=response.request_id,
        status=workflow.reconciliation_status,
        balance_delta_rub=balance_delta,
        recorded_net_spend_rub=recorded_net_spend,
        difference_rub=difference,
    )


async def _advance_live(workflow: Workflow, client: VibeClient) -> Workflow:
    if not _settings().vibe_api_token:
        workflow.status = "blocked"
        _audit(workflow, "live_blocked", reason="token_not_configured")
        _save(workflow)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_mode_not_configured",
                "message": (
                    "Live execution requires VIBE_API_TOKEN. No step was marked "
                    "complete and no budget was changed."
                ),
            },
        )

    if any(step.status == "running" for step in workflow.steps):
        workflow.status = "running"
        _save(workflow)
        return deepcopy(workflow)

    workflow.status = "running"
    for index, step in enumerate(workflow.steps):
        if step.status in {"complete", "skipped"}:
            continue
        if step.status == "awaiting_approval":
            workflow.status = "awaiting_approval"
            _save(workflow)
            return deepcopy(workflow)
        if step.kind == "guard":
            next_step = next(
                (
                    candidate
                    for candidate in workflow.steps[index + 1 :]
                    if candidate.kind == "generation"
                    and candidate.status not in {"complete", "skipped"}
                ),
                None,
            )
            if next_step is not None:
                _prepare_dependent_payload(workflow, next_step)
                try:
                    estimate_response = await _runtime_estimate_with_fallbacks(
                        client, workflow, next_step
                    )
                except (VibeAPIError, CatalogError) as exc:
                    step.status = "error"
                    step.error_code = getattr(exc, "code", "estimate_invalid")
                    step.error_message = str(exc)
                    workflow.status = "error"
                    _audit(
                        workflow,
                        "precharge_guard_failed",
                        step=step,
                        request_id=getattr(exc, "request_id", None),
                    )
                    _save(workflow)
                    raise
                if next_step.status != "skipped":
                    _apply_runtime_approval_policy(workflow, next_step)
                step.upstream_request_id = (
                    estimate_response.request_id if estimate_response else None
                )
                _audit(
                    workflow,
                    "precharge_guard_passed",
                    step=step,
                    request_id=(
                        estimate_response.request_id if estimate_response else None
                    ),
                    next_step=next_step.id,
                    fresh_estimate_rub=next_step.estimated_cost_rub,
                    approval_required=next_step.requires_approval,
                    scope_reduced=next_step.status == "skipped",
                )
            step.status = "complete"
            continue

        _prepare_dependent_payload(workflow, step)
        try:
            estimate_response = await _runtime_estimate_with_fallbacks(
                client, workflow, step
            )
        except (VibeAPIError, CatalogError) as exc:
            step.status = "error"
            step.error_code = getattr(exc, "code", "estimate_invalid")
            step.error_message = str(exc)
            workflow.status = "error"
            _audit(
                workflow,
                "estimate_failed",
                step=step,
                request_id=getattr(exc, "request_id", None),
            )
            _save(workflow)
            raise

        if step.status == "skipped":
            continue

        _apply_runtime_approval_policy(workflow, step)
        _audit(
            workflow,
            "runtime_estimate_confirmed",
            step=step,
            request_id=estimate_response.request_id if estimate_response else None,
            estimate_rub=step.estimated_cost_rub,
        )

        spendable_budget = max(0, workflow.remaining_budget_rub - workflow.reserve_rub)
        if step.estimated_cost_rub > spendable_budget:
            if step.requires_approval:
                step.status = "skipped"
                step.error_code = "budget_guard"
                step.error_message = (
                    "Optional step does not fit the remaining envelope."
                )
                _audit(workflow, "step_downgraded_to_skip", step=step)
                continue
            step.status = "blocked"
            workflow.status = "blocked"
            _audit(workflow, "budget_guard_blocked", step=step)
            _save(workflow)
            return deepcopy(workflow)

        if step.requires_approval and not _approval_is_current(workflow, step):
            step.status = "awaiting_approval"
            workflow.status = "awaiting_approval"
            _audit(
                workflow,
                "approval_requested",
                step=step,
                threshold_rub=workflow.approval_required_above_rub,
                estimate_rub=step.estimated_cost_rub,
                estimate_drift_rub=step.estimate_drift_rub,
                drift_tolerance_rub=workflow.price_drift_tolerance_rub,
            )
            _save(workflow)
            return deepcopy(workflow)

        fingerprint = _fingerprint(step.request_payload)
        step.request_fingerprint = fingerprint
        step.idempotency_key = step.idempotency_key or _idempotency_key(
            workflow, step, fingerprint
        )
        step.status = "running"
        _save(workflow)
        try:
            response = await client.generate(
                step.request_payload, idempotency_key=step.idempotency_key
            )
        except VibeAPIError as exc:
            step.status = "error"
            step.error_code = exc.code
            step.error_message = exc.message
            step.upstream_request_id = exc.request_id
            workflow.status = "error"
            _audit(
                workflow,
                "generation_rejected",
                step=step,
                request_id=exc.request_id,
                charged_rub=0,
            )
            _save(workflow)
            raise

        _record_generation_result(step, response)
        _sync_workflow_balance(workflow, response.data)
        _recompute_costs(workflow)
        status = str(response.data.get("status", "processing"))
        _audit(
            workflow,
            "generation_accepted",
            step=step,
            request_id=response.request_id,
            generation_id=step.generation_id,
            charged_rub=step.actual_cost_rub,
            replayed=bool(response.data.get("replayed", False)),
        )
        if status == "complete":
            step.status = "complete"
            continue
        if status in {"pending", "processing", "running"} and step.generation_id:
            step.status = "running"
            workflow.status = "running"
            _save(workflow)
            return deepcopy(workflow)

        step.status = "error"
        step.error_code = str(response.data.get("error") or "unexpected_status")
        step.error_message = str(
            response.data.get("error_message") or f"Unexpected status: {status}"
        )
        workflow.status = "error"
        _save(workflow)
        return deepcopy(workflow)

    workflow.status = "complete"
    workflow.execution_verified = all(
        step.status in {"complete", "skipped"}
        and (step.kind == "guard" or step.status == "skipped" or step.generation_id)
        for step in workflow.steps
    )
    await _reconcile_balance(workflow, client)
    _audit(workflow, "workflow_complete", verified=workflow.execution_verified)
    _recompute_costs(workflow)
    _save(workflow)
    return deepcopy(workflow)


def _advance_demo(workflow: Workflow) -> Workflow:
    workflow.status = "running"
    projected = 0.0
    for step in workflow.steps:
        if step.status in {"simulated", "complete", "skipped"}:
            if step.status == "simulated":
                projected += step.estimated_cost_rub
            continue
        if step.kind == "guard":
            step.status = "simulated"
            step.simulated = True
            _audit(workflow, "guard_simulated", step=step, charged_rub=0)
            continue
        if step.requires_approval and step.approved_at is None:
            step.status = "awaiting_approval"
            workflow.status = "awaiting_approval"
            _audit(
                workflow,
                "approval_requested_in_simulation",
                step=step,
                threshold_rub=workflow.approval_required_above_rub,
                estimate_rub=step.estimated_cost_rub,
            )
            break
        if (
            projected + step.estimated_cost_rub
            > workflow.budget_rub - workflow.reserve_rub
        ):
            step.status = "skipped"
            _audit(workflow, "simulated_scope_reduction", step=step)
            continue
        step.status = "simulated"
        step.simulated = True
        projected += step.estimated_cost_rub
        _audit(workflow, "generation_simulated", step=step, charged_rub=0)
    else:
        workflow.status = "complete"

    workflow.actual_spend_rub = 0
    workflow.refunded_rub = 0
    workflow.remaining_budget_rub = workflow.budget_rub
    workflow.projected_remaining_budget_rub = round(workflow.budget_rub - projected, 2)
    workflow.execution_verified = False
    _save(workflow)
    return deepcopy(workflow)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "VibePilot API",
        "version": API_VERSION,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "default_execution_mode": "demo",
        "token_configured": bool(_settings().vibe_api_token),
        "live_control_configured": bool(_settings().live_control_key),
        "live_mode_is_explicit": True,
        "state_store": _store().engine.dialect.name,
        "webhook_configured": bool(
            _settings().public_base_url and _settings().vibe_webhook_secret
        ),
        "receipt_signing": "ephemeral" if _signer().ephemeral else "configured",
        "version": API_VERSION,
        "timestamp": _utcnow().isoformat(),
    }


@app.get("/api/v1/integrations/vibe/health")
async def vibe_health(
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> dict[str, object]:
    if not _settings().vibe_api_token:
        return {
            "status": "unconfigured",
            "connected": False,
            "live_ready": False,
            "message": (
                "VIBE_API_TOKEN is not configured; only explicit demo runs work."
            ),
        }
    started = time.perf_counter()
    async with _client() as client:
        me, balance, capabilities = (
            await client.me(),
            await client.balance(),
            await client.capabilities(),
        )
    model_groups = capabilities.data.get("models", {})
    model_count = sum(
        len(group) for group in model_groups.values() if isinstance(group, dict)
    )
    return {
        "status": "ok",
        "connected": True,
        "live_ready": bool(_settings().live_control_key),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "balance_rub": _extract_balance(balance.data)
        if _has_live_access(x_vibepilot_live_key)
        else "redacted",
        "model_count": model_count,
        "scopes": me.data.get("scopes") or me.data.get("granted"),
        "request_ids": [
            request_id
            for request_id in (
                me.request_id,
                balance.request_id,
                capabilities.request_id,
            )
            if request_id
        ],
        "webhook_ready": bool(
            _settings().public_base_url and _settings().vibe_webhook_secret
        ),
    }


@app.post("/api/v1/webhooks/vibe")
async def vibe_webhook(
    request: Request,
    x_vibe_signature: str | None = Header(default=None, alias="X-Vibe-Signature"),
    x_vibe_event: str | None = Header(default=None, alias="X-Vibe-Event"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict[str, Any]:
    secret = _settings().vibe_webhook_secret
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "webhook_not_configured",
                "message": "VIBE_WEBHOOK_SECRET is not configured.",
            },
        )
    raw_body = await request.body()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    presented = (x_vibe_signature or "").removeprefix("sha256=")
    if not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_webhook_signature"},
        )
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_webhook_json"}
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"code": "invalid_webhook_payload"})
    event = x_vibe_event or str(payload.get("event", "unknown"))
    if event == "webhook.test":
        return {"status": "verified", "event": event}
    generation_id = payload.get("generation_id")
    if generation_id is None:
        raise HTTPException(status_code=422, detail={"code": "generation_id_required"})
    link = _store().find_workflow_by_generation(generation_id)
    if link is None:
        return {
            "status": "ignored",
            "reason": "generation_not_owned_by_vibepilot",
        }
    workflow_id, step_id = link[0].id, link[1]
    async with _workflow_lock(workflow_id):
        workflow = _get(workflow_id)
        step = next(item for item in workflow.steps if item.id == step_id)
        incoming_status = str(payload.get("status", ""))
        if step.status == "complete" and incoming_status == "complete":
            return {
                "status": "already_applied",
                "workflow_id": workflow.id,
                "step_id": step.id,
            }
        if step.status == "error" and (
            incoming_status == "error" or event == "generation.error"
        ):
            return {
                "status": "already_applied",
                "workflow_id": workflow.id,
                "step_id": step.id,
            }
        response = VibeResponse(payload, x_request_id, 200)
        _record_generation_result(step, response)
        if incoming_status == "complete" or event == "generation.complete":
            step.status = "complete"
            _audit(
                workflow,
                "generation_completed_by_signed_webhook",
                step=step,
                request_id=x_request_id,
                generation_id=generation_id,
                webhook_attempt=payload.get("attempt"),
            )
            _recompute_costs(workflow)
            _save(workflow)
            async with _client() as client:
                advanced = await _advance_live(workflow, client)
            return {
                "status": "applied",
                "workflow_id": workflow.id,
                "step_id": step.id,
                "workflow_status": advanced.status,
            }

        if incoming_status != "error" and event != "generation.error":
            raise HTTPException(
                status_code=422,
                detail={"code": "unsupported_webhook_event", "event": event},
            )

        step.status = "error"
        step.error_code = str(payload.get("error") or "generation_failed")
        step.error_message = str(payload.get("error_message") or "Generation failed.")
        if payload.get("refunded") is True:
            step.refunded_rub = step.actual_cost_rub
        workflow.status = "error"
        _audit(
            workflow,
            "generation_failed_by_signed_webhook",
            step=step,
            request_id=x_request_id,
            generation_id=generation_id,
            refunded_rub=step.refunded_rub,
        )
        _recompute_costs(workflow)
        _save(workflow)
        return {
            "status": "applied",
            "workflow_id": workflow.id,
            "step_id": step.id,
            "workflow_status": workflow.status,
        }


@app.get("/api/v1/integrations/vibe/catalog")
async def vibe_catalog() -> dict[str, object]:
    async with _client() as client:
        response = await client.capabilities()
    groups = response.data.get("models", {})
    return {
        "source": "GET /api/agent/capabilities",
        "request_id": response.request_id,
        "models": {
            group: {
                name: {
                    key: spec.get(key)
                    for key in ("price", "description", "required", "optional")
                }
                for name, spec in entries.items()
            }
            for group, entries in groups.items()
            if isinstance(entries, dict)
        },
        "text_models": response.data.get("text_models", {}).get("models", {}),
    }


@app.post("/api/v1/integrations/vibe/webhook-test")
async def test_vibe_webhook_delivery(
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> dict[str, Any]:
    _require_live_access(x_vibepilot_live_key)
    settings = _settings()
    if not settings.public_base_url or not settings.vibe_webhook_secret:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "webhook_not_configured",
                "message": (
                    "PUBLIC_BASE_URL and VIBE_WEBHOOK_SECRET are required for "
                    "the signed delivery self-test."
                ),
            },
        )
    callback_url = f"{settings.public_base_url}/api/v1/webhooks/vibe"
    async with _client() as client:
        response = await client.webhook_test(callback_url)
    return {
        "callback_url": callback_url,
        "request_id": response.request_id,
        **response.data,
    }


def _contract_pricer(client: VibeClient, approval_threshold: float) -> Any:
    cache: dict[str, dict[str, Any]] = {}

    async def price(steps: list[Step]) -> None:
        for step in steps:
            if step.kind != "generation":
                continue
            _attach_callback(step)
            fingerprint = _fingerprint(step.request_payload)
            cached = cache.get(fingerprint)
            if cached is not None:
                cost = estimate_cost_from_response(cached)
                step.initial_estimated_cost_rub = cost
                step.estimated_cost_rub = cost
                step.cost_rub = cost
                step.pricing_source = "vibe_estimate"
                step.estimate_snapshot = deepcopy(cached)
            else:
                response = await _estimate_step(client, step)
                cache[fingerprint] = deepcopy(response.data)
            apply_approval_policy(step, approval_threshold)

    return price


async def _build_contract_artifact(
    request: BudgetComparisonRequest,
    *,
    compile_contract: bool,
) -> BudgetComparison | BudgetContract:
    if any(budget < 10 or budget > 50_000 for budget in request.budgets_rub):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_budget_range",
                "message": "Every budget must be between 10 and 50,000 rubles.",
            },
        )
    async with _client() as client:
        capabilities = await client.capabilities()
        pricer = None
        if request.execution_mode == "live":
            await client.me()
            pricer = _contract_pricer(client, request.approval_required_above_rub)
        try:
            if compile_contract:
                return await build_contract(
                    catalog=capabilities.data,
                    request=request,
                    price_steps=pricer,
                )
            return await build_comparison(
                catalog=capabilities.data,
                request=request,
                price_steps=pricer,
            )
        except (CatalogError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "budget_contract_not_feasible",
                    "message": str(exc),
                    "charged_rub": 0,
                },
            ) from exc


@app.post("/api/v1/budget/compare", response_model=BudgetComparison)
async def compare_budgets(
    request: BudgetComparisonRequest,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> BudgetComparison:
    if request.execution_mode == "live":
        _require_live_access(x_vibepilot_live_key)
    result = await _build_contract_artifact(request, compile_contract=False)
    if not isinstance(result, BudgetComparison):
        raise AssertionError("comparison builder returned the wrong artifact")
    return result


@app.post("/api/v1/contracts/compile", response_model=BudgetContract)
async def compile_budget_contract(
    request: BudgetComparisonRequest,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> BudgetContract:
    if request.execution_mode == "live":
        _require_live_access(x_vibepilot_live_key)
    result = await _build_contract_artifact(request, compile_contract=True)
    if not isinstance(result, BudgetContract):
        raise AssertionError("contract builder returned the wrong artifact")
    _store().save_contract(result)
    return result


@app.get("/api/v1/contracts/{contract_id}", response_model=BudgetContract)
def get_budget_contract(
    contract_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> BudgetContract:
    contract = _store().get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Budget Contract not found.")
    if contract.mode == "live":
        _require_live_access(x_vibepilot_live_key)
    return contract


@app.post("/api/v1/contracts/{contract_id}/activate", response_model=Workflow)
async def activate_budget_contract(
    contract_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    contract = _store().get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Budget Contract not found.")
    if contract.mode == "live":
        _require_live_access(x_vibepilot_live_key)
    if not hmac.compare_digest(budget_contract_digest(contract), contract.digest):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "budget_contract_integrity_failed",
                "message": "Stored Budget Contract no longer matches its digest.",
                "charged_rub": 0,
            },
        )
    if contract.workflow_id:
        return deepcopy(_get(contract.workflow_id))

    balance_before = None
    current_catalog_digest = contract.catalog_digest
    if contract.mode == "live":
        async with _client() as client:
            balance_before = _extract_balance((await client.balance()).data)
            current_catalog_digest = catalog_digest((await client.capabilities()).data)
    adaptations: list[str] = []
    if current_catalog_digest != contract.catalog_digest:
        adaptations.append(
            "Catalog changed after compilation; every step will be re-estimated "
            "before spend and approval will be invalidated on excessive drift."
        )
    workflow = _new_workflow(
        brief=contract.brief,
        budget_rub=contract.policy.max_total_rub,
        priority=contract.selected_scenario.priority,
        mode=contract.mode,
        approval_required_above_rub=contract.policy.approval_above_rub,
        reserve_rub=contract.policy.reserve_rub,
        price_drift_tolerance_rub=contract.policy.price_drift_tolerance_rub,
        include_video=contract.selected_scenario.include_video,
        steps=contract.steps,
        catalog_fingerprint=contract.catalog_digest,
        balance_before=balance_before,
        adaptations=adaptations,
        contract=contract,
    )
    _audit(
        workflow,
        "budget_contract_activated",
        contract_id=contract.id,
        contract_digest=contract.digest,
        catalog_changed=current_catalog_digest != contract.catalog_digest,
    )
    _save(workflow)
    contract.status = "activated"
    contract.activated_at = _utcnow()
    contract.workflow_id = workflow.id
    _store().save_contract(contract)
    return deepcopy(workflow)


@app.post("/api/v1/workflows/plan", response_model=Workflow)
async def plan_workflow(
    request: CampaignRequest,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    if request.execution_mode == "live":
        _require_live_access(x_vibepilot_live_key)

    async with _client() as client:
        capabilities = await client.capabilities()
        balance_before: float | None = None
        me_response: VibeResponse | None = None
        if request.execution_mode == "live":
            me_response = await client.me()
            balance_response = await client.balance()
            balance_before = _extract_balance(balance_response.data)
        try:
            steps = await _priced_steps(client, capabilities.data, request)
        except CatalogError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "catalog_or_estimate_invalid", "message": str(exc)},
            ) from exc

        required, maximum = _workflow_costs(steps)
        adaptations: list[str] = []
        effective_priority = request.priority
        if (
            required + request.reserve_rub > request.budget_rub
            and request.priority != "economy"
        ):
            economy_request = request.model_copy(update={"priority": "economy"})
            try:
                steps = await _priced_steps(client, capabilities.data, economy_request)
            except CatalogError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "catalog_or_estimate_invalid", "message": str(exc)},
                ) from exc
            required, maximum = _workflow_costs(steps)
            effective_priority = "economy"
            adaptations.append(
                "Priority was automatically reduced to economy before any spend."
            )

    if required + request.reserve_rub > request.budget_rub:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "budget_too_low",
                "message": "Бюджета недостаточно для обязательных шагов.",
                "minimum_budget_rub": round(required + request.reserve_rub, 2),
                "reserved_safety_margin_rub": request.reserve_rub,
                "charged_rub": 0,
            },
        )
    if maximum + request.reserve_rub > request.budget_rub:
        adaptations.append(
            "Optional steps that do not fit will be skipped instead of overspending."
        )

    workflow = _new_workflow(
        brief=request.brief,
        budget_rub=request.budget_rub,
        priority=effective_priority,
        mode=request.execution_mode,
        approval_required_above_rub=request.approval_required_above_rub,
        reserve_rub=request.reserve_rub,
        price_drift_tolerance_rub=request.price_drift_tolerance_rub,
        include_video=request.include_video,
        steps=steps,
        catalog_fingerprint=catalog_digest(capabilities.data),
        balance_before=balance_before,
        adaptations=adaptations,
    )
    _audit(
        workflow,
        "workflow_planned",
        request_id=capabilities.request_id,
        pricing=(
            "live /generate/estimate"
            if request.execution_mode == "live"
            else "live public /capabilities (simulation only)"
        ),
        me_request_id=me_response.request_id if me_response else None,
    )
    _save(workflow)
    return deepcopy(workflow)


@app.post("/api/v1/workflows/{workflow_id}/execute", response_model=Workflow)
@_locked_workflow
async def execute_workflow(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    workflow = _get(workflow_id)
    if workflow.status in {"complete", "cancelled"}:
        return deepcopy(workflow)
    if workflow.mode == "demo":
        return _advance_demo(workflow)
    _require_live_access(x_vibepilot_live_key)
    async with _client() as client:
        return await _advance_live(workflow, client)


@app.post("/api/v1/workflows/{workflow_id}/refresh", response_model=Workflow)
@_locked_workflow
async def refresh_workflow(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    workflow = _get(workflow_id)
    if workflow.mode != "live":
        return deepcopy(workflow)
    _require_live_access(x_vibepilot_live_key)
    running = next((step for step in workflow.steps if step.status == "running"), None)
    async with _client() as client:
        if running is None:
            return await _advance_live(workflow, client)
        if not running.generation_id:
            running.status = "error"
            running.error_code = "missing_generation_id"
            running.error_message = (
                "A live async step cannot be verified without generation_id."
            )
            workflow.status = "error"
            _save(workflow)
            return deepcopy(workflow)

        try:
            response = await client.generation_status(running.generation_id)
        except VibeAPIError as exc:
            _audit(
                workflow,
                "poll_failed",
                step=running,
                request_id=exc.request_id,
                upstream_code=exc.code,
            )
            _save(workflow)
            raise
        _record_generation_result(running, response)
        _sync_workflow_balance(workflow, response.data)
        status = str(response.data.get("status", "processing"))
        if status in {"pending", "processing", "running"}:
            _audit(
                workflow,
                "generation_polled",
                step=running,
                request_id=response.request_id,
                status=status,
            )
            _recompute_costs(workflow)
            _save(workflow)
            return deepcopy(workflow)

        if status == "complete":
            running.status = "complete"
            _audit(
                workflow,
                "generation_verified_complete",
                step=running,
                request_id=response.request_id,
                generation_id=running.generation_id,
                actual_cost_rub=running.actual_cost_rub,
            )
            _recompute_costs(workflow)
            return await _advance_live(workflow, client)

        running.status = "error"
        running.error_code = str(response.data.get("error") or "generation_failed")
        running.error_message = str(
            response.data.get("error_message") or "Generation failed."
        )
        if response.data.get("refunded") is True:
            running.refunded_rub = running.actual_cost_rub
        workflow.status = "error"
        _audit(
            workflow,
            "generation_verified_error",
            step=running,
            request_id=response.request_id,
            refunded_rub=running.refunded_rub,
        )
        _recompute_costs(workflow)
        _save(workflow)
        return deepcopy(workflow)


@app.post("/api/v1/workflows/{workflow_id}/reconcile", response_model=Workflow)
@_locked_workflow
async def reconcile_workflow(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    workflow = _get(workflow_id)
    if workflow.mode != "live":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "simulation_cannot_be_reconciled",
                "message": "Demo workflows never spend and have no live balance delta.",
            },
        )
    _require_live_access(x_vibepilot_live_key)
    async with _client() as client:
        await _reconcile_balance(workflow, client)
    _save(workflow)
    return deepcopy(workflow)


@app.post("/api/v1/workflows/{workflow_id}/approve", response_model=Workflow)
@_locked_workflow
async def approve_workflow(
    workflow_id: str,
    request: ApprovalRequest,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    workflow = _get(workflow_id)
    if workflow.mode == "live":
        _require_live_access(x_vibepilot_live_key)
    step = next(
        (item for item in workflow.steps if item.status == "awaiting_approval"),
        None,
    )
    if step is None:
        raise HTTPException(
            status_code=409, detail="Workflow is not awaiting approval."
        )
    if not request.approved:
        step.status = "skipped"
        step.approval_note = request.note
        workflow.status = "cancelled"
        _audit(workflow, "approval_rejected", step=step, note=request.note)
        _save(workflow)
        return deepcopy(workflow)

    step.approved_at = _utcnow()
    step.approval_note = request.note
    step.approved_estimate_rub = step.estimated_cost_rub
    step.approved_request_fingerprint = _fingerprint(step.request_payload)
    step.status = "planned"
    _audit(
        workflow,
        "approval_granted",
        step=step,
        threshold_rub=workflow.approval_required_above_rub,
        estimate_rub=step.estimated_cost_rub,
        note=request.note,
    )
    if workflow.mode == "demo":
        return _advance_demo(workflow)
    async with _client() as client:
        return await _advance_live(workflow, client)


@app.get("/api/v1/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> Workflow:
    workflow = _get(workflow_id)
    if workflow.mode == "live":
        _require_live_access(x_vibepilot_live_key)
    return deepcopy(workflow)


def _receipt(workflow: Workflow) -> dict[str, Any]:
    if workflow.mode == "demo":
        verification = "simulation"
    elif workflow.status == "complete" and workflow.execution_verified:
        verification = "verified"
    elif workflow.status in {"error", "blocked", "cancelled"}:
        verification = "failed_or_stopped"
    else:
        verification = "in_progress"
    body: dict[str, Any] = {
        "receipt_version": 2,
        "workflow_id": workflow.id,
        "trace_id": workflow.trace_id,
        "contract_id": workflow.contract_id,
        "contract_digest": workflow.contract_digest,
        "catalog_digest": workflow.catalog_digest,
        "mode": workflow.mode,
        "verification": verification,
        "status": workflow.status,
        "created_at": workflow.created_at.isoformat(),
        "updated_at": workflow.updated_at.isoformat(),
        "budget": {
            "envelope_rub": workflow.budget_rub,
            "estimated_max_rub": workflow.maximum_cost_rub,
            "reserved_safety_margin_rub": workflow.reserve_rub,
            "actual_charged_rub": workflow.actual_spend_rub,
            "refunded_rub": workflow.refunded_rub,
            "net_spend_rub": round(
                workflow.actual_spend_rub - workflow.refunded_rub, 2
            ),
            "balance_before_rub": workflow.balance_before_rub,
            "balance_after_rub": workflow.balance_after_rub,
        },
        "reconciliation": {
            "status": workflow.reconciliation_status,
            "reconciled_at": (
                workflow.reconciled_at.isoformat() if workflow.reconciled_at else None
            ),
            "balance_delta_rub": workflow.reconciled_balance_delta_rub,
            "recorded_net_spend_rub": round(
                workflow.actual_spend_rub - workflow.refunded_rub, 2
            ),
            "difference_rub": workflow.reconciliation_difference_rub,
            "note": (
                "A mismatch can also indicate concurrent spending on the same "
                "VibeMarketolog account; generation-level costs remain recorded."
            ),
        },
        "approval_policy": {
            "threshold_rub": workflow.approval_required_above_rub,
            "price_drift_tolerance_rub": workflow.price_drift_tolerance_rub,
            "decision_source": (
                "fresh estimate > threshold OR price drift > tolerance"
            ),
        },
        "steps": [
            {
                "id": step.id,
                "status": step.status,
                "model": step.model,
                "pricing_source": step.pricing_source,
                "initial_estimated_cost_rub": step.initial_estimated_cost_rub,
                "estimated_cost_rub": step.estimated_cost_rub,
                "estimate_drift_rub": step.estimate_drift_rub,
                "actual_cost_rub": step.actual_cost_rub,
                "refunded_rub": step.refunded_rub,
                "requires_approval": step.requires_approval,
                "approved_at": step.approved_at.isoformat()
                if step.approved_at
                else None,
                "approved_estimate_rub": step.approved_estimate_rub,
                "approved_request_fingerprint": (step.approved_request_fingerprint),
                "generation_id": step.generation_id,
                "task_id": step.task_id,
                "upstream_request_id": step.upstream_request_id,
                "request_fingerprint": step.request_fingerprint,
                "idempotency_key": step.idempotency_key,
                "applied_fallback": step.applied_fallback,
                "approval_reason": step.approval_reason,
                "approval_note": step.approval_note,
                "result_url": step.result_url,
                "structured_evaluation": step.structured_result,
                "error_code": step.error_code,
                "error_message": step.error_message,
                "simulated": step.simulated,
            }
            for step in workflow.steps
        ],
    }
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    body["integrity"] = {
        "algorithm": "sha256",
        "digest": hashlib.sha256(canonical).hexdigest(),
        "scope": "all receipt fields except integrity",
    }
    body["signature"] = _signer().sign(body)
    return body


@app.get("/api/v1/receipts/public-key")
def get_receipt_public_key() -> dict[str, Any]:
    return _signer().public_document()


@app.post("/api/v1/receipts/verify")
def verify_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    public_document = _signer().public_document()
    valid, message = verify_signed_receipt(
        receipt,
        expected_key_id=str(public_document["key_id"]),
        expected_public_key_base64url=str(public_document["public_key_base64url"]),
    )
    signature = receipt.get("signature")
    return {
        "valid": valid,
        "message": message,
        "identity_pinned": True,
        "workflow_id": receipt.get("workflow_id"),
        "key_id": signature.get("key_id") if isinstance(signature, dict) else None,
    }


@app.get("/api/v1/workflows/{workflow_id}/receipt")
def get_workflow_receipt(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None, alias="X-VibePilot-Live-Key"
    ),
) -> dict[str, Any]:
    workflow = _get(workflow_id)
    if workflow.mode == "live":
        _require_live_access(x_vibepilot_live_key)
    return _receipt(workflow)
