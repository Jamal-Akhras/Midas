"""Tool infrastructure.

Every tool result is stored as a DataSnapshot BEFORE the model sees it -
the audit trail must show the data the decision saw. The executor wraps
raw tool data with source + retrieved_at so agents can cite both.
"""
import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from midas.models import DataSnapshot, utcnow
from midas.providers.base import ToolSpec

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """A tool could not produce data. Reported to the model as data,
    never invented around (honest-data rule)."""


@dataclass
class ToolResult:
    data: Any
    source: str  # alpaca|yfinance|edgar|finnhub|fmp|brave|tavily|web|demo


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., ToolResult]

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description,
                        parameters=self.parameters)


class ToolContext:
    """Binds tools to a DB session + run for snapshot provenance. Collects
    snapshot ids so the pipeline can stamp them with the consuming artifact."""

    def __init__(self, db: Session, *, run_id=None, ticker: str = ""):
        self.db = db
        self.run_id = run_id
        self.ticker = ticker
        self.snapshot_ids: list = []

    def executor(self, tools: dict[str, Tool]) -> Callable[[str, dict], Any]:
        def execute(name: str, arguments: dict) -> Any:
            tool = tools.get(name)
            if tool is None:
                raise ToolError(f"unknown tool '{name}'")
            retrieved_at = utcnow()
            try:
                result = tool.fn(**arguments)
                ok, err, data, source = True, "", result.data, result.source
            except TypeError as exc:  # bad arguments from the model
                ok, err, data, source = False, f"bad arguments: {exc}", None, "none"
            except ToolError as exc:
                ok, err, data, source = False, str(exc), None, "none"
            snap = DataSnapshot(
                run_id=self.run_id,
                ticker=(arguments.get("ticker") or self.ticker or "").upper(),
                tool_name=name, params=arguments, data=data, source=source,
                retrieved_at=retrieved_at, ok=ok, error=err,
            )
            self.db.add(snap)
            self.db.flush()
            self.snapshot_ids.append(snap.id)
            if self.run_id is not None:
                # commit per snapshot so the UI can watch a stage work in
                # real time (artifacts drive crash-resume, not snapshots -
                # early durability here is harmless). Ad-hoc contexts
                # (monitor quote checks) have no run_id and keep their
                # caller's transaction boundaries.
                try:
                    self.db.commit()
                except Exception:
                    pass
            if not ok:
                raise ToolError(err)
            return {
                "source": source,
                "retrieved_at": retrieved_at.isoformat(),
                "data": data,
            }
        return execute

    def stamp_artifact(self, artifact_id) -> None:
        """Link every snapshot gathered so far to the artifact that used it."""
        if not self.snapshot_ids:
            return
        for snap in self.db.query(DataSnapshot).filter(
            DataSnapshot.id.in_(self.snapshot_ids)
        ):
            snap.artifact_id = artifact_id
        self.db.flush()
        self.snapshot_ids = []


def params(props: dict[str, dict], required: list[str] | None = None) -> dict:
    """Terse JSON-schema builder for tool parameters."""
    return {"type": "object", "properties": props, "required": required or []}
