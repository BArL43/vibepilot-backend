from datetime import UTC, datetime, timedelta

from app.models import Workflow
from app.store import StateStore


def workflow(workflow_id: str, updated_at: datetime) -> Workflow:
    return Workflow(
        id=workflow_id,
        brief=f"Campaign {workflow_id} with enough text for the workflow brief.",
        budget_rub=100,
        priority="balanced",
        status="complete",
        required_cost_rub=0,
        maximum_cost_rub=0,
        remaining_budget_rub=80,
        projected_remaining_budget_rub=80,
        actual_spend_rub=20,
        refunded_rub=0,
        approval_required_above_rub=10,
        reserve_rub=5,
        price_drift_tolerance_rub=2,
        include_video=False,
        steps=[],
        created_at=updated_at - timedelta(minutes=1),
        updated_at=updated_at,
        mode="demo",
        trace_id=f"trace-{workflow_id}",
    )


def test_list_workflows_returns_recent_campaigns_first() -> None:
    store = StateStore("sqlite:///:memory:")
    now = datetime.now(UTC)
    store.save_workflow(workflow("wf_old", now - timedelta(hours=1)))
    store.save_workflow(workflow("wf_new", now))

    history = store.list_workflows(limit=10)

    assert [item.id for item in history] == ["wf_new", "wf_old"]
    assert history[0].actual_spend_rub == 20


def test_list_workflows_respects_limit() -> None:
    store = StateStore("sqlite:///:memory:")
    now = datetime.now(UTC)
    store.save_workflow(workflow("wf_one", now - timedelta(minutes=2)))
    store.save_workflow(workflow("wf_two", now - timedelta(minutes=1)))
    store.save_workflow(workflow("wf_three", now))

    history = store.list_workflows(limit=2)

    assert [item.id for item in history] == ["wf_three", "wf_two"]
