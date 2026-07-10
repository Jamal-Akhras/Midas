"""Committee output schemas. Deterministic quality gates live in validators;
a violation triggers the router's repair path.

Honest-data rule, schema-enforced where possible: research claims carry
sources; anything without a source belongs in `unverified`.
"""
from pydantic import BaseModel, Field, model_validator


# ---------- screener ----------

class ScreenCandidate(BaseModel):
    ticker: str
    setup: str                      # which configured setup it matches
    score: float = Field(ge=0, le=100)
    price: float
    avg_volume: float
    rationale: str
    data_points: list[str] = []     # raw numbers cited from tool results


class ScreenOutput(BaseModel):
    candidates: list[ScreenCandidate]
    universe_size: int
    notes: str = ""


# ---------- fundamentals ----------

class EarningsQuarter(BaseModel):
    period: str                     # e.g. "2026Q1"
    eps_actual: float | None = None
    eps_estimate: float | None = None
    surprise_pct: float | None = None


class FundamentalsOutput(BaseModel):
    ticker: str
    recent_quarters: list[EarningsQuarter]
    next_earnings_date: str = ""    # ISO date or "" if unknown
    projected_eps_next_q: float | None = None
    projected_revenue_growth_pct: float | None = None
    key_ratios: dict[str, float] = {}
    guidance_history: str = ""
    unverified: list[str] = []      # anything a tool call could not confirm


# ---------- deep research ----------

class SourcedClaim(BaseModel):
    claim: str
    source_url: str
    retrieved_at: str               # ISO timestamp from the tool result

    @model_validator(mode="after")
    def _must_have_source(self) -> "SourcedClaim":
        if not self.source_url.strip():
            raise ValueError("every claim in a dossier must carry a source URL/filing reference")
        return self


class DossierSection(BaseModel):
    topic: str                      # filings|management|insiders|pipeline|coverage
    claims: list[SourcedClaim]


class DossierOutput(BaseModel):
    ticker: str
    sections: list[DossierSection]
    management_quality_notes: str = ""
    insider_activity_summary: str = ""
    unverified: list[str] = []      # model-memory claims are forbidden as fact; they go here
    overall_take: str = ""

    @model_validator(mode="after")
    def _nonempty(self) -> "DossierOutput":
        if not self.sections:
            raise ValueError("a dossier needs at least one sourced section")
        return self


# ---------- catalysts ----------

class Catalyst(BaseModel):
    event: str
    date: str = ""                  # ISO date or "" if unscheduled
    expected_impact: str            # high|medium|low
    source_url: str = ""


class CatalystOutput(BaseModel):
    ticker: str
    upcoming_earnings_date: str = ""
    catalysts: list[Catalyst]
    sector_context: str = ""
    unverified: list[str] = []


# ---------- passable gate (cheap-model triage) ----------

class TriageOutput(BaseModel):
    ticker: str
    passable: bool
    reasons: list[str]
    data_quality_flags: list[str] = []


# ---------- bull / bear ----------

class DirectionalCase(BaseModel):
    ticker: str
    stance: str                     # bull|bear
    conviction: float = Field(ge=0, le=10)
    core_argument: str
    key_evidence: list[str]         # references to dossier/fundamentals claims
    expected_move_pct: float        # signed; bull positive, bear negative
    expected_timeframe_days: int = Field(gt=0)
    what_would_change_my_mind: list[str]


# ---------- options structurer (LLM assist; hard checks are code) ----------

class StructureProposal(BaseModel):
    strategy: str                   # long_call|long_put|call_debit_spread|put_debit_spread
    expiry: str                     # ISO date
    long_strike: float
    short_strike: float | None = None
    rationale: str


class StructureAssistOutput(BaseModel):
    ticker: str
    picked: StructureProposal
    alternatives_considered: list[StructureProposal] = []


# ---------- investor verdict ----------

class ReviewSchedule(BaseModel):
    cadence_days: int = Field(gt=0, default=1)
    event_triggers: list[str] = ["earnings", "invalidation", "dte"]


class VerdictOutput(BaseModel):
    ticker: str
    verdict: str                    # trade|no_trade
    direction: str = ""             # bullish|bearish (required when trade)
    instrument: str = ""            # shares|option (required when trade)
    entry_zone_low: float | None = None
    entry_zone_high: float | None = None
    stop_price: float | None = None
    targets: list[float] = []
    thesis_summary: str
    what_invalidates_this: list[str] = []
    review_schedule: ReviewSchedule = ReviewSchedule()
    no_trade_reason: str = ""

    @model_validator(mode="after")
    def _trade_fields(self) -> "VerdictOutput":
        # Models sometimes answer 'long/short' or 'stock/options'; map the
        # unambiguous synonyms deterministically, reject anything else so
        # the router's repair attempt fixes it.
        dir_map = {"long": "bullish", "short": "bearish",
                   "bull": "bullish", "bear": "bearish"}
        inst_map = {"options": "option", "stock": "shares",
                    "equity": "shares", "share": "shares"}
        if self.direction:
            d = self.direction.strip().lower()
            self.direction = dir_map.get(d, d)
        if self.instrument:
            i = self.instrument.strip().lower()
            self.instrument = inst_map.get(i, i)
        if self.verdict == "trade":
            if self.direction not in ("bullish", "bearish"):
                raise ValueError("direction must be 'bullish' or 'bearish'")
            if self.instrument not in ("shares", "option"):
                raise ValueError("instrument must be 'shares' or 'option'")
            missing = [f for f, v in {
                "direction": self.direction, "instrument": self.instrument,
                "entry_zone_low": self.entry_zone_low,
                "entry_zone_high": self.entry_zone_high,
                "stop_price": self.stop_price,
            }.items() if not v]
            if missing:
                raise ValueError(f"a 'trade' verdict requires: {missing}")
            if not self.what_invalidates_this:
                raise ValueError("a 'trade' verdict must state what invalidates it")
        elif self.verdict == "no_trade":
            if not self.no_trade_reason:
                raise ValueError("a 'no_trade' verdict must state why")
        else:
            raise ValueError("verdict must be 'trade' or 'no_trade'")
        return self


# ---------- position re-evaluation ----------

class PostMortemOutput(BaseModel):
    ticker: str
    what_worked: list[str] = []
    what_failed: list[str] = []
    lesson: str  # ONE transferable sentence future committees will read

    @model_validator(mode="after")
    def _has_lesson(self) -> "PostMortemOutput":
        if not self.lesson.strip():
            raise ValueError("a post-mortem without a lesson is a eulogy")
        return self


class PortfolioAction(BaseModel):
    ticker: str
    decision: str                   # hold|trim|exit
    reasons: list[str] = []

    @model_validator(mode="after")
    def _valid(self) -> "PortfolioAction":
        self.ticker = self.ticker.upper().strip()
        d = self.decision.lower().strip()
        d = {"sell": "exit", "close": "exit", "reduce": "trim",
             "keep": "hold", "maintain": "hold"}.get(d, d)
        if d not in ("hold", "trim", "exit"):
            raise ValueError("decision must be hold|trim|exit")
        self.decision = d
        if d != "hold" and not self.reasons:
            raise ValueError("trim/exit requires explicit reasons")
        return self


class PortfolioReviewOutput(BaseModel):
    assessment: str                 # one-paragraph book-level view
    actions: list[PortfolioAction]
    rebalance_note: str = ""        # concentration / correlation observations


class ReevaluationOutput(BaseModel):
    ticker: str
    decision: str                   # hold|trim|exit|roll
    reasons: list[str]
    thesis_still_valid: bool
    time_pressure_note: str = ""    # required thinking for options as DTE decays
    next_review_days: int = Field(gt=0, default=1)

    @model_validator(mode="after")
    def _valid_decision(self) -> "ReevaluationOutput":
        if self.decision not in ("hold", "trim", "exit", "roll"):
            raise ValueError("decision must be hold|trim|exit|roll")
        if not self.reasons:
            raise ValueError("every position decision needs explicit reasons")
        return self
