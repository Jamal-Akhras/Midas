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
ZONE_MIN_ATR = 0.1  # the fillable part of the zone, in ATRs (2026-11-20)
TARGET_CAP_ATR = 1.0  # a pullback's first target: within this of its 20-day high


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
        "(the swing low in the technical block), or rule no_trade"
    )


def _atr_problem(stop: float, reference: float, atr: float | None) -> str | None:
    """NVDA 2026-11-11: a 7% stop in a stock moving 4-5% a day; AXP: levels
    with no structure behind them. A stop inside one day's range is hit by
    noise. The ATR comes from the technical block; without one, no check.
    `reference` is the fill nearest the stop that the zone allows (INTU
    2026-11-17: checked from the zone high, filled a fifth of an ATR away)."""
    floor = float(settings.min_stop_atr)
    if atr is None or atr <= 0 or floor <= 0:
        return None
    distance = abs(reference - stop) / atr
    if distance >= floor:
        return None
    return (
        f"stop {stop:g} is {distance:.2f} ATR from the lowest fill {reference:g} "
        f"(14-day ATR {atr:g}; "
        f"minimum {floor:g} ATR) - a stop inside one day's range is hit by noise, not by a "
        "broken thesis; put it under the swing low in the technical block"
    )


def _earnings_problem(verdict: VerdictOutput, earnings: tuple[int, str] | None) -> str | None:
    """NVDA 2026-11-11: bought six days before earnings with the event as
    the thesis, at full size. Inside the entry window a trade verdict must
    say it is an earnings play (and is then sized at half risk) or rule
    no_trade."""
    window = int(settings.entry_earnings_blackout_days)
    if earnings is None or window <= 0:
        return None
    days, date_str = earnings
    if days < 0 or days > window or verdict.earnings_play:
        return None
    return (
        f"earnings on {date_str} is {days} day(s) away, inside the {window}-day entry window: "
        "rule no_trade, or set earnings_play true to hold through the event at half size and "
        "say so in the thesis"
    )


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _window_problem(
    lo: float, hi: float, last_close: float | None, atr: float | None
) -> str | None:
    """2026-11-20: the executor fills only between the prior close and the
    zone high. SNPS, WFC and INTU all carried zones under the last close
    (WFC's a single price), which under that rule can never fill - and
    before it, filled only on a day the reclaim had already failed."""
    if last_close is None or last_close <= 0:
        return None
    window = max(hi - max(lo, last_close), 0.0)
    if atr is not None and atr > 0:
        if window / atr >= ZONE_MIN_ATR:
            return None
        width = f"that window is {window / atr:.2f} ATR wide (minimum {ZONE_MIN_ATR:g})"
    elif window > 0:
        return None
    else:
        width = "that window is empty"
    return (
        f"entry zone {lo:g}-{hi:g} cannot fill: an order fills only between the prior close "
        f"{last_close:g} and the zone high, while the reclaim is still standing, and {width} "
        "- set the zone from the last close up, or rule no_trade"
    )


def _target_cap_problem(verdict: VerdictOutput, technical: dict, atr: float | None) -> str | None:
    """A thin trade must not be rescued by a further target: the first
    target of a pullback is the retest of its 20-day high. (The other
    escape, a lower entry zone, is `_window_problem`.)"""
    floor = float(settings.min_reward_to_risk)
    if technical.get("setup") != "pullback-in-uptrend" or not verdict.targets:
        return None
    high = _num((technical.get("high_20d") or {}).get("price"))
    if high is None or atr is None or atr <= 0 or floor <= 0:
        return None
    target = float(verdict.targets[0])
    beyond = (target - high) / atr
    if beyond <= TARGET_CAP_ATR:
        return None
    return (
        f"first target {target:g} is {beyond:.1f} ATR beyond the 20-day high {high:g}: the "
        f"first target of a pullback is the retest of that high (within {TARGET_CAP_ATR:g} "
        "ATR of it); a further level is a second target - if the retest does not pay "
        f"{floor:g} to 1, rule no_trade"
    )


def problems(
    verdict: VerdictOutput,
    spot: float,
    *,
    atr: float | None = None,
    earnings: tuple[int, str] | None = None,
    technical: dict | None = None,
) -> list[str]:
    """Empty list = sane. Only trade verdicts are checked. `atr` is the
    technical block's 14-day ATR; `earnings` is (days away, ISO date) from
    the run's own artifacts; `technical` is the block itself, for the
    entry-on-strength checks (the last close, the setup, the 20-day high)."""
    if verdict.verdict != "trade":
        return []
    lo = float(verdict.entry_zone_low or 0.0)
    hi = float(verdict.entry_zone_high or 0.0)
    stop = float(verdict.stop_price or 0.0)
    bullish = verdict.direction == "bullish"
    out: list[str] = []
    tech = technical if (technical and settings.entry_require_strength) else {}
    last_close = _num(tech.get("close"))
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
        # the fill nearest the stop: the zone low, or the prior close when
        # the executor will not fill under it
        lowest_fill = max(lo, last_close) if last_close else lo
        if stop >= lo:
            out.append(f"stop {stop:g} is not below the entry zone {lo:g}-{hi:g}")
        else:
            distance = (reference - stop) / reference * 100.0
            if distance < STOP_MIN_PCT:
                out.append(f"stop is {distance:.1f}% below entry (minimum {STOP_MIN_PCT:g}%)")
            elif distance > STOP_MAX_PCT:
                out.append(f"stop is {distance:.0f}% below entry (maximum {STOP_MAX_PCT:g}%)")
            else:
                atr_problem = _atr_problem(stop, lowest_fill, atr)
                if atr_problem:
                    out.append(atr_problem)
        window_problem = _window_problem(lo, hi, last_close, atr)
        if window_problem:
            out.append(window_problem)
        bad_targets = [t for t in verdict.targets if float(t) <= hi]
        if bad_targets:
            out.append(f"targets {bad_targets} are not above the entry zone")
        elif stop < lo:
            rr_problem = _reward_to_risk_problem(verdict, reference, stop)
            if rr_problem:
                out.append(rr_problem)
            cap_problem = _target_cap_problem(verdict, tech, atr)
            if cap_problem:
                out.append(cap_problem)
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
            else:
                atr_problem = _atr_problem(stop, reference, atr)
                if atr_problem:
                    out.append(atr_problem)
        bad_targets = [t for t in verdict.targets if float(t) >= lo]
        if bad_targets:
            out.append(f"targets {bad_targets} are not below the entry zone")
        elif stop > hi:
            rr_problem = _reward_to_risk_problem(verdict, reference, stop)
            if rr_problem:
                out.append(rr_problem)
    earnings_problem = _earnings_problem(verdict, earnings)
    if earnings_problem:
        out.append(earnings_problem)
    return out
