"""Deterministic option-structure checks: liquidity, spread width, DTE,
premium sanity. The LLM assist picks only from candidates that pass here,
and its pick is re-validated afterwards. Hard checks in CODE, always."""

from dataclasses import dataclass
from typing import Any

from midas.config import settings


@dataclass
class ChainContract:
    expiry: str
    dte: int
    type: str  # call | put
    strike: float
    bid: float
    ask: float
    mid: float
    open_interest: int
    volume: int
    iv: float | None = None
    delta: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ChainContract":
        fields: dict[str, Any] = {
            k: d.get(k)
            for k in (
                "expiry",
                "dte",
                "type",
                "strike",
                "bid",
                "ask",
                "mid",
                "open_interest",
                "volume",
                "iv",
                "delta",
            )
        }
        return cls(**fields)


def contract_checks(c: ChainContract) -> dict[str, dict]:
    """Named checks for one contract. All must pass for candidacy."""
    checks: dict[str, dict] = {}
    checks["min_dte"] = {
        "ok": c.dte >= settings.min_dte_at_entry,
        "value": c.dte,
        "limit": f">= {settings.min_dte_at_entry}",
    }
    checks["has_market"] = {
        "ok": c.bid > 0 and c.ask > c.bid,
        "value": f"bid {c.bid} / ask {c.ask}",
        "limit": "two-sided market",
    }
    checks["open_interest"] = {
        "ok": c.open_interest >= settings.min_open_interest,
        "value": c.open_interest,
        "limit": f">= {settings.min_open_interest}",
    }
    spread_pct = ((c.ask - c.bid) / c.mid * 100) if c.mid else 999.0
    checks["bid_ask_spread"] = {
        "ok": spread_pct <= settings.max_bid_ask_spread_pct,
        "value": round(spread_pct, 2),
        "limit": f"<= {settings.max_bid_ask_spread_pct}%",
    }
    return checks


def passes(c: ChainContract) -> bool:
    return all(v["ok"] for v in contract_checks(c).values())


def failure_histogram(chain: list[dict]) -> dict:
    """Why did contracts die? Counts every failed check across the whole
    chain so a 'no valid structure' verdict is diagnosable, not a shrug."""
    hist: dict[str, int] = {}
    for d in chain:
        try:
            c = ChainContract.from_dict(d)
        except TypeError:
            hist["unparseable"] = hist.get("unparseable", 0) + 1
            continue
        for name, chk in contract_checks(c).items():
            if not chk["ok"]:
                hist[name] = hist.get(name, 0) + 1
    return {"contracts_seen": len(chain), "failed_checks": hist}


def filter_chain(chain: list[dict], option_type: str) -> list[ChainContract]:
    """Contracts of the right type that pass every hard check, preferring
    the configured DTE window (falls back to legal-but-not-preferred)."""
    typed = [ChainContract.from_dict(d) for d in chain if d.get("type") == option_type]
    legal = [c for c in typed if passes(c)]
    preferred = [
        c for c in legal if settings.preferred_dte_min <= c.dte <= settings.preferred_dte_max
    ]
    return preferred or legal


def candidate_structures(
    chain: list[dict],
    *,
    direction: str,
    spot: float,
    expected_move_pct: float,
    account_equity: float,
) -> list[dict]:
    """Code-generated candidate structures for the LLM assist to choose
    among. Long single legs near-the-money plus one debit spread targeting
    the expected move. Every candidate passed the hard checks."""
    option_type = "call" if direction == "bullish" else "put"
    legal = filter_chain(chain, option_type)
    if not legal:
        return []
    max_premium = account_equity * settings.max_premium_pct_per_trade / 100

    by_expiry: dict[str, list[ChainContract]] = {}
    for c in legal:
        by_expiry.setdefault(c.expiry, []).append(c)

    candidates: list[dict] = []
    for expiry, contracts in sorted(by_expiry.items()):
        contracts.sort(key=lambda c: abs(c.strike - spot))
        atm = contracts[0]
        # long single leg (premium must fit the per-trade cap)
        if atm.mid * 100 <= max_premium:
            candidates.append(
                {
                    "strategy": f"long_{option_type}",
                    "expiry": expiry,
                    "dte": atm.dte,
                    "long_strike": atm.strike,
                    "short_strike": None,
                    "est_premium_per_contract": atm.mid,
                    "liquidity": {
                        "open_interest": atm.open_interest,
                        "spread_pct": round((atm.ask - atm.bid) / atm.mid * 100, 2)
                        if atm.mid
                        else None,
                    },
                    "checks": contract_checks(atm),
                }
            )
        # debit spread: short leg near the expected-move target
        target = spot * (1 + expected_move_pct / 100)
        shorts = [
            c
            for c in contracts
            if (c.strike > atm.strike if option_type == "call" else c.strike < atm.strike)
        ]
        if shorts:
            short = min(shorts, key=lambda c: abs(c.strike - target))
            debit = round(atm.mid - short.mid, 4)
            if debit > 0 and debit * 100 <= max_premium:
                candidates.append(
                    {
                        "strategy": f"{option_type}_debit_spread",
                        "expiry": expiry,
                        "dte": atm.dte,
                        "long_strike": atm.strike,
                        "short_strike": short.strike,
                        "est_premium_per_contract": debit,
                        "liquidity": {
                            "open_interest": min(atm.open_interest, short.open_interest),
                            "spread_pct": None,
                        },
                        "checks": {
                            "long_leg": contract_checks(atm),
                            "short_leg": contract_checks(short),
                        },
                    }
                )
    return candidates


def validate_pick(pick: dict, candidates: list[dict]) -> dict | None:
    """The LLM's pick must exactly match a code-generated candidate
    (strategy, expiry, strikes). Off-list picks are rejected."""
    for cand in candidates:
        if (
            pick.get("strategy") == cand["strategy"]
            and pick.get("expiry") == cand["expiry"]
            and float(pick.get("long_strike", -1)) == float(cand["long_strike"])
            and (pick.get("short_strike") or None) == (cand["short_strike"] or None)
        ):
            return cand
    return None
