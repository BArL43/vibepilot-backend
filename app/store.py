from __future__ import annotations

from threading import RLock

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app.models import BudgetContract, Workflow


class StoreConflict(RuntimeError):
    pass


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


class StateStore:
    """JSON state with optimistic revisions and a generation lookup index."""

    def __init__(self, database_url: str) -> None:
        normalized = _normalize_database_url(database_url)
        engine_kwargs: dict[str, object] = {"pool_pre_ping": True}
        if normalized == "sqlite:///:memory:":
            engine_kwargs.update(
                {
                    "connect_args": {"check_same_thread": False},
                    "poolclass": StaticPool,
                }
            )
        elif normalized.startswith("sqlite:"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        self.engine: Engine = create_engine(normalized, **engine_kwargs)
        self.metadata = MetaData()
        self.workflows = Table(
            "workflows",
            self.metadata,
            Column("id", String(80), primary_key=True),
            Column("payload", Text, nullable=False),
            Column("revision", Integer, nullable=False),
        )
        self.contracts = Table(
            "budget_contracts",
            self.metadata,
            Column("id", String(80), primary_key=True),
            Column("payload", Text, nullable=False),
            Column("revision", Integer, nullable=False),
        )
        self.generation_links = Table(
            "generation_links",
            self.metadata,
            Column("generation_id", String(120), primary_key=True),
            Column("workflow_id", String(80), nullable=False, index=True),
            Column("step_id", String(80), nullable=False),
        )
        self.metadata.create_all(self.engine)
        self._lock = RLock()

    def save_workflow(self, workflow: Workflow) -> None:
        with self._lock, self.engine.begin() as connection:
            current = connection.execute(
                select(self.workflows.c.revision).where(
                    self.workflows.c.id == workflow.id
                )
            ).scalar_one_or_none()
            if current is None:
                if workflow.revision != 0:
                    raise StoreConflict(
                        f"Workflow {workflow.id} disappeared before revision "
                        f"{workflow.revision} could be saved"
                    )
                workflow.revision = 1
                connection.execute(
                    insert(self.workflows).values(
                        id=workflow.id,
                        payload=workflow.model_dump_json(),
                        revision=workflow.revision,
                    )
                )
            else:
                if current != workflow.revision:
                    raise StoreConflict(
                        f"Workflow {workflow.id} revision conflict: "
                        f"expected {workflow.revision}, stored {current}"
                    )
                previous = workflow.revision
                workflow.revision += 1
                result = connection.execute(
                    update(self.workflows)
                    .where(
                        self.workflows.c.id == workflow.id,
                        self.workflows.c.revision == previous,
                    )
                    .values(
                        payload=workflow.model_dump_json(),
                        revision=workflow.revision,
                    )
                )
                if result.rowcount != 1:
                    raise StoreConflict(f"Workflow {workflow.id} concurrent update")

            connection.execute(
                delete(self.generation_links).where(
                    self.generation_links.c.workflow_id == workflow.id
                )
            )
            links = [
                {
                    "generation_id": str(step.generation_id),
                    "workflow_id": workflow.id,
                    "step_id": step.id,
                }
                for step in workflow.steps
                if step.generation_id is not None
            ]
            if links:
                connection.execute(insert(self.generation_links), links)

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.workflows.c.payload).where(
                    self.workflows.c.id == workflow_id
                )
            ).scalar_one_or_none()
        return Workflow.model_validate_json(payload) if payload else None

    def list_workflows(self, limit: int = 50) -> list[Workflow]:
        safe_limit = max(1, min(int(limit), 200))
        with self.engine.connect() as connection:
            payloads = (
                connection.execute(select(self.workflows.c.payload)).scalars().all()
            )

        workflows = [
            Workflow.model_validate_json(payload) for payload in payloads if payload
        ]
        workflows.sort(key=lambda item: item.updated_at, reverse=True)
        return workflows[:safe_limit]

    def workflow_count(self) -> int:
        with self.engine.connect() as connection:
            return len(connection.execute(select(self.workflows.c.id)).all())

    def find_workflow_by_generation(
        self, generation_id: int | str
    ) -> tuple[Workflow, str] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    self.generation_links.c.workflow_id,
                    self.generation_links.c.step_id,
                ).where(self.generation_links.c.generation_id == str(generation_id))
            ).one_or_none()
        if row is None:
            return None
        workflow = self.get_workflow(row.workflow_id)
        return (workflow, row.step_id) if workflow else None

    def save_contract(self, contract: BudgetContract) -> None:
        with self._lock, self.engine.begin() as connection:
            current = connection.execute(
                select(self.contracts.c.revision).where(
                    self.contracts.c.id == contract.id
                )
            ).scalar_one_or_none()
            if current is None:
                if contract.revision != 0:
                    raise StoreConflict(f"Contract {contract.id} revision conflict")
                contract.revision = 1
                connection.execute(
                    insert(self.contracts).values(
                        id=contract.id,
                        payload=contract.model_dump_json(),
                        revision=contract.revision,
                    )
                )
            else:
                if current != contract.revision:
                    raise StoreConflict(f"Contract {contract.id} revision conflict")
                previous = contract.revision
                contract.revision += 1
                result = connection.execute(
                    update(self.contracts)
                    .where(
                        self.contracts.c.id == contract.id,
                        self.contracts.c.revision == previous,
                    )
                    .values(
                        payload=contract.model_dump_json(),
                        revision=contract.revision,
                    )
                )
                if result.rowcount != 1:
                    raise StoreConflict(f"Contract {contract.id} concurrent update")

    def get_contract(self, contract_id: str) -> BudgetContract | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.contracts.c.payload).where(
                    self.contracts.c.id == contract_id
                )
            ).scalar_one_or_none()
        return BudgetContract.model_validate_json(payload) if payload else None

    def clear(self) -> None:
        with self._lock, self.engine.begin() as connection:
            connection.execute(delete(self.generation_links))
            connection.execute(delete(self.workflows))
            connection.execute(delete(self.contracts))
