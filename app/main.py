from __future__ import annotations

import os
import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


API_VERSION = "0.1.0"
VIBE_API_BASE = os.getenv(
    "VIBE_API_BASE", "https://lk.vibemarketolog.ru/api/agent"
).rstrip("/")
VIBE_API_TOKEN = os.getenv("VIBE_API_TOKEN", "")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"


def _cors_origins() -> list[str]:
    configured = [
        item.strip()
        for item in os.getenv("CORS_ORIGINS", "").split(",")
        if item.strip()
    ]
    if configured:
        return configured
    return ["*"] if DEMO_MODE else []


app = FastAPI(
    title="VibePilot API",
    description="Budget-aware workflow orchestration for VibeMarketolog Agent API.",
    version=API_VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class CampaignRequest(BaseModel):
    brief: str = Field(min_length=20, max_length=4_000)
    budget_rub: int = Field(default=250, ge=20, le=50_000)
    priority: Literal["economy", "balanced", "quality"] = "balanced"


class ApprovalRequest(BaseModel):
    approved: bool = True


class Step(BaseModel):
    id: str
    title: str
    purpose: str
    model: str
    cost_rub: int
    duration_hint: str
    status: Literal[
        "planned", "running", "complete", "awaiting_approval", "skipped", "error"
    ] = "planned"
    reason: str
    requires_approval: bool = False


class Workflow(BaseModel):
    id: str
    brief: str
    budget_rub: int
    priority: str
    status: Literal[
        "planned", "running", "awaiting_approval", "complete", "cancelled"
    ]
    required_cost_rub: int
    maximum_cost_rub: int
    remaining_budget_rub: int
    approval_required_above_rub: int
    steps: list[Step]
    created_at: datetime
    updated_at: datetime
    mode: Literal["demo", "live"]
    trace_id: str


WORKFLOWS: dict[str, Workflow] = {}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _steps(priority: str) -> list[Step]:
    quality = priority == "quality"
    economy = priority == "economy"
    return [
        Step(
            id="concepts",
            title="Концепции",
            purpose="Три рекламные гипотезы из бизнес-брифа",
            model="text-balanced",
            cost_rub=7 if economy else 9,
            duration_hint="≈ 8 сек",
            reason="Достаточное качество для широкого divergent-поиска.",
        ),
        Step(
            id="selection",
            title="Отбор",
            purpose="Оценка концепций по фиксированной рубрике",
            model="text-critic",
            cost_rub=8 if not quality else 12,
            duration_hint="≈ 6 сек",
            reason="Отдельная модель снижает self-evaluation bias.",
        ),
        Step(
            id="banner",
            title="Баннер",
            purpose="Квадратный креатив победившей концепции",
            model="image-cyrillic",
            cost_rub=29 if economy else 35,
            duration_hint="≈ 45 сек",
            reason="Поддерживает кириллицу и укладывается в бюджет.",
        ),
        Step(
            id="qa",
            title="QA",
            purpose="Проверка оффера, читаемости и брендовых рисков",
            model="vision-fast",
            cost_rub=5,
            duration_hint="≈ 5 сек",
            reason="Быстрый независимый контроль перед дорогим шагом.",
        ),
        Step(
            id="video",
            title="Видео",
            purpose="Короткая видеоадаптация одобренного баннера",
            model="video-fast",
            cost_rub=120 if not quality else 160,
            duration_hint="≈ 3–8 мин",
            reason="Самый дорогой шаг вынесен за human approval gate.",
            requires_approval=True,
        ),
    ]


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
        "mode": "demo" if DEMO_MODE or not VIBE_API_TOKEN else "live",
        "version": API_VERSION,
        "timestamp": _utcnow().isoformat(),
    }


@app.get("/api/v1/integrations/vibe/health")
async def vibe_health() -> dict[str, object]:
    if not VIBE_API_TOKEN:
        return {
            "status": "demo",
            "connected": False,
            "message": "VIBE_API_TOKEN is not configured; safe demo mode is active.",
        }

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(
                f"{VIBE_API_BASE}/balance",
                headers={"Authorization": f"Bearer {VIBE_API_TOKEN}"},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"VibeMarketolog health check failed: {type(exc).__name__}",
        ) from exc

    return {
        "status": "ok",
        "connected": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


@app.post("/api/v1/workflows/plan", response_model=Workflow)
def plan_workflow(request: CampaignRequest) -> Workflow:
    steps = _steps(request.priority)
    required_cost = sum(step.cost_rub for step in steps if not step.requires_approval)
    maximum_cost = sum(step.cost_rub for step in steps)
    if request.budget_rub < required_cost:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "budget_too_low",
                "message": "Бюджета недостаточно для обязательных шагов.",
                "minimum_budget_rub": required_cost,
            },
        )

    workflow_id = f"wf_{uuid.uuid4().hex[:12]}"
    now = _utcnow()
    workflow = Workflow(
        id=workflow_id,
        brief=request.brief,
        budget_rub=request.budget_rub,
        priority=request.priority,
        status="planned",
        required_cost_rub=required_cost,
        maximum_cost_rub=maximum_cost,
        remaining_budget_rub=request.budget_rub,
        approval_required_above_rub=80,
        steps=steps,
        created_at=now,
        updated_at=now,
        mode="demo" if DEMO_MODE or not VIBE_API_TOKEN else "live",
        trace_id=uuid.uuid4().hex,
    )
    WORKFLOWS[workflow_id] = workflow
    return deepcopy(workflow)


@app.post("/api/v1/workflows/{workflow_id}/execute", response_model=Workflow)
def execute_workflow(workflow_id: str) -> Workflow:
    workflow = WORKFLOWS.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    if workflow.status not in {"planned", "running"}:
        return deepcopy(workflow)

    workflow.status = "running"
    spent = 0
    for step in workflow.steps:
        if step.requires_approval:
            if workflow.budget_rub - spent < step.cost_rub:
                step.status = "skipped"
                workflow.status = "complete"
            else:
                step.status = "awaiting_approval"
                workflow.status = "awaiting_approval"
            break
        step.status = "complete"
        spent += step.cost_rub

    workflow.remaining_budget_rub = workflow.budget_rub - spent
    workflow.updated_at = _utcnow()
    return deepcopy(workflow)


@app.post("/api/v1/workflows/{workflow_id}/approve", response_model=Workflow)
def approve_workflow(workflow_id: str, request: ApprovalRequest) -> Workflow:
    workflow = WORKFLOWS.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    if workflow.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Workflow is not awaiting approval.")

    video = next(step for step in workflow.steps if step.id == "video")
    if not request.approved:
        video.status = "skipped"
        workflow.status = "cancelled"
    elif workflow.remaining_budget_rub < video.cost_rub:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "budget_exceeded",
                "message": "Недостаточно остатка бюджета для видео.",
            },
        )
    else:
        video.status = "complete"
        workflow.remaining_budget_rub -= video.cost_rub
        workflow.status = "complete"

    workflow.updated_at = _utcnow()
    return deepcopy(workflow)


@app.get("/api/v1/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(workflow_id: str) -> Workflow:
    workflow = WORKFLOWS.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return deepcopy(workflow)
