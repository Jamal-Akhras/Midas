"""2026-11-12: evidence is tagged with where it came from, and the
catalyst agent stops dressing analyst notes and price moves as catalysts.

The NVDA catalysts were a downgrade, a reiteration and "why is NVDA
gaining Wednesday"; the bull, bear and verdict cited nothing. Now each
key_evidence item names its artifact, code counts how many do, and the
investor is told the count - an untagged claim is opinion."""

from sqlalchemy import select

from midas.agents.defs import PROMPTS_DIR
from midas.config import settings
from midas.models import ArtifactType, CostEvent, ModelCall, ResearchArtifact
from midas.pipeline import committee

AXP_BULL = [
    "AXP's earnings beat with EPS of 4.68 vs. 4.53 estimate, a 3.34% surprise (fundamentals data)",
    "Positive catalysts from the acquisition of TheFork and Canadian card expansion (catalysts data)",
    "Price target increase by President Capital to $406 (dossier news)",
]
OPINION = ["momentum is strong", "sentiment is improving across the sector"]


def test_audit_counts_items_that_name_an_artifact():
    audit = committee.evidence_audit(AXP_BULL)
    assert audit == {"total": 3, "sourced": 3, "unsourced": []}
    audit = committee.evidence_audit(OPINION)
    assert audit["sourced"] == 0 and audit["unsourced"] == OPINION
    mixed = committee.evidence_audit(["10-Q revenue +18% YoY", "CEO $4.6M purchase"])
    assert mixed["sourced"] == 1 and mixed["unsourced"] == ["CEO $4.6M purchase"]
    assert committee.evidence_audit([]) == {"total": 0, "sourced": 0, "unsourced": []}


def test_audit_note_reads_as_a_count():
    note = committee.evidence_note(
        committee.evidence_audit(AXP_BULL), committee.evidence_audit(OPINION)
    )
    assert note.startswith("EVIDENCE AUDIT: bull 3/3 items name an artifact; bear 0/2")
    assert "opinion" in note
    assert "momentum is strong" in note


def test_the_committee_tells_the_investor_the_count(factory, demo_router, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "autonomy_level", 0)
    committee.run_committee(factory, demo_router, "ACME")
    with factory() as db:
        risk = db.scalar(
            select(ResearchArtifact).where(
                ResearchArtifact.artifact_type == ArtifactType.risk_check
            )
        ).content
        # demo bull: 10-Q, 8-K, "CEO $4.6M purchase"; demo bear: 10-K, 8-K, forward P/E
        assert risk["evidence_audit"]["bull"]["sourced"] == 2
        assert risk["evidence_audit"]["bear"]["sourced"] == 2
        assert risk["evidence_note"].startswith("EVIDENCE AUDIT: bull 2/3")
        prompts = db.execute(
            select(ModelCall.user_prompt)
            .join(CostEvent, ModelCall.cost_event_id == CostEvent.id)
            .where(CostEvent.agent_slug == "investor")
        ).all()
        assert prompts and all("EVIDENCE AUDIT: bull 2/3" in p[0] for p in prompts)


def test_prompts_carry_the_rules():
    catalyst = (PROMPTS_DIR / "catalyst.md").read_text(encoding="utf-8")
    assert "are NOT catalysts" in catalyst
    for name in ("bull", "bear"):
        text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
        assert "<artifact>: <claim>" in text
    investor = (PROMPTS_DIR / "investor.md").read_text(encoding="utf-8")
    assert "EVIDENCE AUDIT" in investor
