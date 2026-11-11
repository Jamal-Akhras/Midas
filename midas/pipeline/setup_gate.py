"""The setup gate: the swing setups' definitions written as numbers.

The screener attaches a setup label to each candidate; this gate checks
the label against the code-computed technical block (see
`committee.technical_block`) and names every way the chart fails to be
what the label says. A chart below its 50-day average, too far off its
20-day high, or down too much in five sessions is in a downtrend, not a
pullback, whatever base it broke out of last month; a "pullback" on
expanding volume is distribution.

The gate removes candidates; it never adds one, and it never punishes
missing data - an unknown technical state passes, and the committee is
told the state is unknown.

Why it exists: on 2026-11-11 the screener called NVDA - 25% off its high,
below its 50-day, down 18% in five sessions on doubled volume after a
downgrade - a "breakout-pullback", triage repeated the label, and the
investor bought it on a conviction tie. Every number the gate checks was
already in the technical block the committee read.
"""

from __future__ import annotations

from midas.config import settings

PULLBACK_SETUPS = ("breakout-pullback",)


def normalise_setup(setup: str) -> str:
    return str(setup or "").strip().lower().replace("_", "-").replace(" ", "-")


def setup_gate(technical: dict | None, setup: str = "") -> list[str]:
    """Why this technical block fails the swing setups; empty means it
    passes. `setup` is the screener's label (any case); the volume rule
    applies only to pullback setups, where contracting volume IS the
    setup."""
    if not settings.setup_gate_enabled or not technical:
        return []
    failures: list[str] = []
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
    if (
        normalise_setup(setup) in PULLBACK_SETUPS
        and ratio is not None
        and float(ratio) > ratio_limit
    ):
        failures.append(
            f"volume {float(ratio):.2f}x its 20-day average into the pullback "
            f"(limit {ratio_limit:.1f}x; a pullback contracts, distribution expands)"
        )
    return failures
