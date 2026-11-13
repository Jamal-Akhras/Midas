"""The setup gate: the swing setups' definitions written as numbers.

The technical block (see `committee.technical_block`) names the setup a
chart IS - `code_setup` below - and this gate names every way the chart
fails to be a swing setup at all. A chart below its 50-day average, too
far off its 20-day high, or down too much in five sessions is in a
downtrend, not a pullback, whatever base it broke out of last month; a
"pullback" on expanding volume is distribution; a pullback that has not
closed back above its 10-day mean and the prior close is still falling.

The gate removes candidates; it never adds one, and it never punishes
missing data - an unknown technical state passes, and the committee is
told the state is unknown.

Why it exists: on 2026-11-11 the screener called NVDA - 25% off its high,
below its 50-day, down 18% in five sessions on doubled volume after a
downgrade - a "breakout-pullback", triage repeated the label, and the
investor bought it on a conviction tie. On 2026-11-12 it labelled all
fifteen deep-scan names "Post-Earnings Drift" with one paragraph; AXP
had reported four weeks earlier and was on its third down day. Every
number the gate checks was already in the technical block the committee
read, so the label is now the code's, and the model's own goes beside it.
"""

from __future__ import annotations

from midas.config import settings

PULLBACK_SETUPS = ("breakout-pullback", "pullback-in-uptrend")
CODE_SETUPS = ("pullback-in-uptrend", "gap-and-hold")
MAX_EXTENSION_PCT = 8.0  # above the 10-day mean: chasing
MAX_BREAKDOWN_PCT = 5.0  # below the 10-day mean: breaking down
GAP_HOLD_SESSIONS = 15  # an up gap older than this is history, not a setup


def normalise_setup(setup: str) -> str:
    return str(setup or "").strip().lower().replace("_", "-").replace(" ", "-")


def _g(x) -> str:
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x)


def _reclaim_miss(technical: dict, *, long: bool) -> str:
    """Why the last close is not a reclaim: under the 10-day mean, or under
    the prior close. `long` is the gate's sentence; the short form goes in
    the setup note and the screener's line."""
    c = float(technical["close"])
    ma10 = technical.get("ma10")
    prior = technical.get("prior_close")
    if ma10 is not None and c <= float(ma10):
        return (
            f"close {_g(c)} is under its 10-day mean {_g(ma10)}"
            if long
            else f"close {_g(c)} under its 10-day {_g(ma10)}"
        )
    return (
        f"close {_g(c)} is under the prior close {_g(prior)}"
        if long
        else f"close {_g(c)} under the prior close {_g(prior)}"
    )


def code_setup(technical: dict | None) -> tuple[str, str]:
    """The setup this technical block IS, as (label, note): one of
    `CODE_SETUPS` or "none" with the reason. Pure; the deep scan's
    definition of an orderly pullback (uptrend, within the gate's pullback
    limit, not extended either way) plus the gap-and-hold: an up gap on
    volume inside `GAP_HOLD_SESSIONS` whose bar low has held."""
    if not technical:
        return "none", "technical state unknown"
    needed = ("close", "ma10", "pullback_from_20d_high_pct", "extension_vs_10d_pct")
    if any(technical.get(k) is None for k in needed):
        return "none", "technical state incomplete"
    c = float(technical["close"])
    ext = float(technical["extension_vs_10d_pct"])
    pullback = float(technical["pullback_from_20d_high_pct"])
    if technical.get("above_ma50") is False:
        return "none", f"below its 50-day average ({_g(c)} vs {_g(technical.get('ma50'))})"
    gap = technical.get("last_gap") or None
    if (
        gap
        and gap.get("direction") == "up"
        and gap.get("holds")
        and int(gap.get("sessions_ago", 10**6)) <= GAP_HOLD_SESSIONS
    ):
        return "gap-and-hold", (
            f"{float(gap['gap_pct']):+.1f}% gap on {float(gap['volume_ratio']):.1f}x volume "
            f"{gap['sessions_ago']} session(s) ago; holding above the gap bar's low "
            f"{_g(gap.get('low'))}"
        )
    if ext > MAX_EXTENSION_PCT:
        return "none", f"extended {ext:.1f}% above its 10-day mean (limit {MAX_EXTENSION_PCT:g}%)"
    if ext < -MAX_BREAKDOWN_PCT:
        return "none", f"{abs(ext):.1f}% below its 10-day mean - breaking down, not pulling back"
    limit = float(settings.setup_gate_max_pullback_pct)
    if pullback > limit:
        return "none", f"{pullback:.1f}% off its 20-day high (limit {limit:.0f}%) - a downtrend"
    head = f"{pullback:.1f}% off the 20-day high in an uptrend"
    reclaim = technical.get("reclaim")
    if reclaim is True:
        return "pullback-in-uptrend", (
            f"{head}; reclaimed (close {_g(c)} above its 10-day {_g(technical.get('ma10'))} "
            f"and the prior close {_g(technical.get('prior_close'))})"
        )
    if reclaim is False:
        return (
            "pullback-in-uptrend",
            f"{head}; no reclaim yet ({_reclaim_miss(technical, long=False)})",
        )
    return "pullback-in-uptrend", head


def facts_line(ticker: str, technical: dict | None, score: float | None = None) -> str:
    """One line per ticker for the screener: the code's setup label and the
    numbers behind it. The screener ranks and explains; it does not label."""
    head = f"{ticker} {float(score):.0f}" if score is not None else ticker
    if not technical:
        return f"{head}: technical state unknown"
    label = str(technical.get("setup") or "none")
    note = str(technical.get("setup_note") or "")
    parts = [f"none ({note})" if label == "none" else label]
    pullback = technical.get("pullback_from_20d_high_pct")
    if pullback is not None:
        parts.append(f"{float(pullback):.1f}% off the 20d high")
    if technical.get("reclaim") is True:
        parts.append("reclaimed")
    elif technical.get("reclaim") is False:
        parts.append(f"no reclaim yet ({_reclaim_miss(technical, long=False)})")
    atr_pct = technical.get("atr14_pct")
    if atr_pct is not None:
        parts.append(f"ATR {float(atr_pct):.1f}%")
    rs = technical.get("rs_20d_vs_spy_pct")
    if rs is not None:
        parts.append(f"RS20 vs SPY {float(rs):+.1f}%")
    return f"{head}: " + "; ".join(parts)


def setup_gate(technical: dict | None, setup: str = "") -> list[str]:
    """Why this technical block fails the swing setups; empty means it
    passes. The block's own `setup` label (code's) outranks `setup` (the
    screener's, any case; kept for blocks written before the code label).
    The volume rule applies only to pullback setups, where contracting
    volume IS the setup; the reclaim rule likewise."""
    if not settings.setup_gate_enabled or not technical:
        return []
    failures: list[str] = []
    code_label = technical.get("setup")
    label = normalise_setup(str(code_label or setup))
    if technical.get("above_ma50") is False:
        failures.append(
            f"below its 50-day average ({technical.get('close')} vs {technical.get('ma50')})"
        )
    pullback = technical.get("pullback_from_20d_high_pct")
    limit = float(settings.setup_gate_max_pullback_pct)
    if pullback is not None and float(pullback) > limit:
        failures.append(f"{float(pullback):.1f}% off its 20-day high (limit {limit:.0f}%)")
    change = technical.get("change_5d_pct")
    drop_limit = float(settings.setup_gate_max_5d_drop_pct)
    if change is not None and float(change) < -drop_limit:
        failures.append(
            f"down {abs(float(change)):.1f}% in five sessions (limit {drop_limit:.0f}%)"
        )
    ratio = technical.get("volume_5d_over_20d")
    ratio_limit = float(settings.setup_gate_max_volume_ratio)
    if label in PULLBACK_SETUPS and ratio is not None and float(ratio) > ratio_limit:
        failures.append(
            f"volume {float(ratio):.2f}x its 20-day average into the pullback "
            f"(limit {ratio_limit:.1f}x; a pullback contracts, distribution expands)"
        )
    if (
        code_label == "pullback-in-uptrend"
        and settings.setup_gate_require_reclaim
        and technical.get("reclaim") is False
    ):
        failures.append(
            f"no reclaim yet: {_reclaim_miss(technical, long=True)} - the pullback is still "
            "falling; a setup needs a close above the 10-day and the prior close"
        )
    if code_label == "none" and not failures:
        failures.append(f"no code-defined setup ({technical.get('setup_note') or 'no setup'})")
    return failures
