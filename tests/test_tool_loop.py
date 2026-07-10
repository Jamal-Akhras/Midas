"""The tool loop: snapshots recorded before the model sees results, tool
errors surfaced as data, every intermediate call cost-logged."""
import json

from pydantic import BaseModel
from sqlalchemy import select

from midas.models import CostEvent, DataSnapshot, PipelineRun, RunKind
from midas.providers.router import ModelRouter
from midas.tools.base import Tool, ToolContext, ToolError, params
from tests.conftest import FakeProvider


class Out(BaseModel):
    summary: str


GOOD = json.dumps({"summary": "done"})


def make_tools():
    def ok_tool(ticker: str):
        from midas.tools.base import ToolResult

        return ToolResult(data={"price": 42.0, "ticker": ticker}, source="demo")

    def broken_tool(ticker: str):
        raise ToolError("data source down")

    return {
        "get_price": Tool("get_price", "price", params({"ticker": {"type": "string"}},
                                                       ["ticker"]), ok_tool),
        "get_broken": Tool("get_broken", "broken", params({"ticker": {"type": "string"}},
                                                          ["ticker"]), broken_tool),
    }


def run_with_script(db, script):
    run = PipelineRun(kind=RunKind.committee, ticker="ACME")
    db.add(run)
    db.flush()
    tools = make_tools()
    ctx = ToolContext(db, run_id=run.id, ticker="ACME")
    provider = FakeProvider("openai", script)
    router = ModelRouter(providers={"openai": provider})
    out = router.run(
        db, task_type="triage", output_schema=Out, system_prompt="s",
        user_prompt="u", tools=[t.spec() for t in tools.values()],
        tool_executor=ctx.executor(tools), run_id=run.id, agent_slug="tester",
    )
    return out, provider, ctx, run


def test_tool_results_are_snapshotted(db):
    out, provider, ctx, run = run_with_script(
        db, [[("get_price", {"ticker": "ACME"})], "ok, done", GOOD])
    assert out.summary == "done"
    snaps = db.scalars(select(DataSnapshot)).all()
    assert len(snaps) == 1
    assert snaps[0].tool_name == "get_price"
    assert snaps[0].data == {"price": 42.0, "ticker": "ACME"}
    assert snaps[0].ok and snaps[0].run_id == run.id
    # the model actually received the snapshot payload
    tool_msg = [m for m in provider.calls[-1]["messages"] if m["role"] == "tool"]
    assert "42.0" in tool_msg[0]["content"]


def test_tool_failure_is_data_not_crash(db):
    out, provider, ctx, run = run_with_script(
        db, [[("get_broken", {"ticker": "ACME"})], "understood", GOOD])
    assert out.summary == "done"
    snap = db.scalars(select(DataSnapshot)).one()
    assert snap.ok is False and "data source down" in snap.error
    tool_msg = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"]
    assert "error" in tool_msg[0]["content"]


def test_every_loop_iteration_is_cost_logged(db):
    run_with_script(db, [[("get_price", {"ticker": "ACME"})], "done text", GOOD])
    events = db.scalars(select(CostEvent)).all()
    # 2 tool-loop calls + 1 final = 3 write-ahead cost events
    assert len(events) == 3
    assert all(e.agent_slug == "tester" for e in events)


def test_artifact_stamping_links_snapshots(db):
    from midas.models import ResearchArtifact, ArtifactType

    out, provider, ctx, run = run_with_script(
        db, [[("get_price", {"ticker": "ACME"})], "done", GOOD])
    art = ResearchArtifact(run_id=run.id, ticker="ACME", agent_slug="tester",
                           artifact_type=ArtifactType.fundamentals, content={})
    db.add(art)
    db.flush()
    ctx.stamp_artifact(art.id)
    snap = db.scalars(select(DataSnapshot)).one()
    assert snap.artifact_id == art.id
