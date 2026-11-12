"""Deterministic sanity checks on an Investor verdict against the live quote.

The schema guarantees the fields exist; it cannot know that an entry zone
of 135-145 with a stop at 20.85 was written for a stock trading at 346.
That happened (GOOGL, 2026-07-17) and the thesis sat waiting for a price
that would never come. These checks reject it and the committee re-prompts
once with the live quote; a second failure ends the run with no thesis.
"""

from __future__ import annotations

from midas.agents.schemas import VerdictOutput
from midas.config import settings

ENTRY_BAND_PCT = 15.0  # entry-zone midpoint must be within this of the live quote
STOP_MIN_PCT = 2.0  # a stop closer than this is noise, not a thesis
STOP_MAX_PCT = 25.0  # a stop further than this is not risk management


def reward_to_risk(entry: float, stop: float, target: float) -> float | None:
    """First-target reward over stop-distance risk, from the worst accepted
    fill; None when the levels do not describe a trade (zero/negative risk)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return abs(target - entry) / risk


def _reward_to_risk_problem(verdict: VerdictOutput, reference: float, stop: float) -> str | None:
    """AXP 2026-11-12: stop 6.0% below entry, first target 3.8% above - 0.6
    to 1 - and nothing in code objected. Shares only: options are sized by
    premium, which is their risk."""
    floor = float(settings.min_reward_to_risk)
    if floor <= 0 or verdict.instrument != "shares":
        return None
    if not verdict.targets:
        return (
            f"a trade verdict needs a first target to check reward-to-risk (minimum {floor:g} to 1)"
        )
    target = float(verdict.targets[0])
    rr = reward_to_risk(reference, stop, target)
    if rr is None or rr >= floor:
        return None
    return (
        f"reward-to-risk is {rr:.2f} to 1 (first target {target:g} vs stop {stop:g} from "
        f"entry {reference:g}; minimum {floor:g}) - put the stop at the structural level "
        f"(the pullback low or just under the 50-day) or raise the target, or rule no_trade"
    )


def problems(verdict: VerdictOutput, spot: float) -> list[str]:
    """Empty list = sane. Only trade verdicts are checked."""
    if verdict.verdict != "trade":
        return []
    lo = float(verdict.entry_zone_low or 0.0)
    hi = float(verdict.entry_zone_high or 0.0)
    stop = float(verdict.stop_price or 0.0)
    bullish = verdict.direction == "bullish"
    out: list[str] = []
    if lo <= 0 or hi <= 0 or stop <= 0:
        return ["entry zone and stop must be positive prices"]
    if lo > hi:
        out.append(f"entry zone is inverted ({lo:g} > {hi:g})")
        lo, hi = hi, lo
    mid = (lo + hi) / 2.0
    if spot > 0:
        deviation = abs(mid / spot - 1.0) * 100.0
        if deviation > ENTRY_BAND_PCT:
            out.append(
                f"entry zone midpoint {mid:.2f} is {deviation:.0f}% from the live quote "
                f"{spot:.2f} (limit {ENTRY_BAND_PCT:g}%)"
            )
    if bullish:
        reference = hi  # the worst fill a bull accepts
        if stop >= lo:
            out.append(f"stop {stop:g} is not below the entry zone {lo:g}-{hi:g}")
        else:
            distance = (reference - stop) / reference * 100.0
            if distance < STOP_MIN_PCT:
                out.append(f"stop is {distance:.1f}% below entry (minimum {STOP_MIN_PCT:g}%)")
            elif distance > STOP_MAX_PCT:
                out.append(f"stop is {distance:.0f}% below entry (maximum {STOP_MAX_PCT:g}%)")
        bad_targets = [t for t in verdict.targets if float(t) <= hi]
        if bad_targets:
            out.append(f"targets {bad_targets} are not above the entry zone")
        elif stop < lo:
            rr_problem = _reward_to_risk_problem(verdict, reference, stop)
            if rr_problem:
                out.append(rr_problem)
    else:
        reference = lo  # the worst fill a bear accepts
        if stop <= hi:
            out.append(f"stop {stop:g} is not above the entry zone {lo:g}-{hi:g}")
        else:
            distance = (stop - reference) / reference * 100.0
            if distance < STOP_MIN_PCT:
                out.append(f"stop is {distance:.1f}% above entry (minimum {STOP_MIN_PCT:g}%)")
            elif distance > STOP_MAX_PCT:
                out.append(f"stop is {distance:.0f}% above entry (maximum {STOP_MAX_PCT:g}%)")
        bad_targets = [t for t in verdict.targets if float(t) >= lo]
        if bad_targets:
            out.append(f"targets {bad_targets} are not below the entry zone")
        elif stop > hi:
            rr_problem = _reward_to_risk_problem(verdict, reference, stop)
            if rr_problem:
                out.append(rr_problem)
    return out
