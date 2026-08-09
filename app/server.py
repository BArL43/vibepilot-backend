from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app import contracts as contracts_module
from app import main as core
from app.v060 import (
    bonus_banner_from,
    booster_state,
    build_steps_v060,
    creative_package,
)

VERSION = "0.6.2"

# Keep the battle-tested v0.5 engine and replace only its planner entry points.
core.build_steps = build_steps_v060
contracts_module.build_steps = build_steps_v060
core.API_VERSION = VERSION
core.app.version = VERSION
app = core.app


def _authorized_workflow(
    workflow_id: str,
    live_key: str | None,
) -> Any:
    workflow = core._get(workflow_id)
    if workflow.mode == "live":
        core._require_live_access(live_key)
    return workflow


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(html)


@app.get("/pilot", include_in_schema=False)
def pilot() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=307)


@app.get("/pilot/", include_in_schema=False)
def pilot_slash() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=307)


@app.get("/api/v1/product")
def product() -> dict[str, Any]:
    return {
        "name": "VibePilot",
        "version": VERSION,
        "features": [
            "budget_contract",
            "text_safe_media",
            "deterministic_russian_copy",
            "budget_booster",
            "signed_receipts",
            "operator_frontend",
            "campaign_history",
            "canonical_frontend",
        ],
        "ui": "/",
    }


@app.get("/api/v1/campaigns")
def list_campaigns(
    limit: int = 50,
    x_vibepilot_live_key: str | None = Header(
        default=None,
        alias="X-VibePilot-Live-Key",
    ),
) -> dict[str, Any]:
    # Campaign history can contain private briefs, spend and media metadata.
    core._require_live_access(x_vibepilot_live_key)

    workflows = core._store().list_workflows(limit=limit)
    items = []
    for workflow in workflows:
        banner = next(
            (
                step
                for step in workflow.steps
                if step.id in {"banner", "bonus_banner"} and step.result_url
            ),
            None,
        )
        video = next(
            (step for step in workflow.steps if step.id == "video" and step.result_url),
            None,
        )
        items.append(
            {
                "id": workflow.id,
                "brief": workflow.brief,
                "status": workflow.status,
                "mode": workflow.mode,
                "priority": workflow.priority,
                "budget_rub": workflow.budget_rub,
                "actual_spend_rub": workflow.actual_spend_rub,
                "refunded_rub": workflow.refunded_rub,
                "net_spend_rub": round(
                    workflow.actual_spend_rub - workflow.refunded_rub,
                    2,
                ),
                "remaining_budget_rub": workflow.remaining_budget_rub,
                "reserve_rub": workflow.reserve_rub,
                "execution_verified": workflow.execution_verified,
                "has_banner": banner is not None,
                "has_video": video is not None,
                "created_at": workflow.created_at,
                "updated_at": workflow.updated_at,
            }
        )

    return {
        "items": items,
        "count": len(items),
        "limit": max(1, min(int(limit), 200)),
    }


@app.get("/api/v1/workflows/{workflow_id}/creative")
def get_creative(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None,
        alias="X-VibePilot-Live-Key",
    ),
) -> dict[str, Any]:
    workflow = _authorized_workflow(workflow_id, x_vibepilot_live_key)
    return creative_package(workflow)


@app.get("/api/v1/workflows/{workflow_id}/booster")
def get_booster(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None,
        alias="X-VibePilot-Live-Key",
    ),
) -> dict[str, Any]:
    workflow = _authorized_workflow(workflow_id, x_vibepilot_live_key)
    return booster_state(workflow)


@app.post("/api/v1/workflows/{workflow_id}/booster")
async def run_booster(
    workflow_id: str,
    x_vibepilot_live_key: str | None = Header(
        default=None,
        alias="X-VibePilot-Live-Key",
    ),
) -> Any:
    async with core._workflow_lock(workflow_id):
        workflow = _authorized_workflow(workflow_id, x_vibepilot_live_key)
        state = booster_state(workflow)
        if not state["eligible"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "budget_booster_not_available",
                    "message": state["reason"],
                    **state,
                },
            )

        bonus = bonus_banner_from(workflow)
        workflow.steps.append(bonus)
        workflow.status = "running"
        workflow.execution_verified = False
        core._audit(
            workflow,
            "budget_booster_added",
            step=bonus,
            available_rub=state["available_rub"],
            expected_rub=state["estimated_extra_rub"],
        )
        core._recompute_costs(workflow)
        core._save(workflow)

        if workflow.mode == "demo":
            return core._advance_demo(workflow)

        async with core._client() as client:
            return await core._advance_live(workflow, client)
