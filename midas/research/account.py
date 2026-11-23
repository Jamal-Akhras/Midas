"""The account as it is configured, replayed over the years the data holds.

Two books share one account, and each is replayed by the code that already
measures it: the core book by `strategies.tactical_sma` (the function the
live book trades) and the swing book by `swing_clone.midas_clone` with the
rules of the live configuration switched on. The swing clone sizes its
positions as a share of the whole account, so its daily return is already
the swing book's contribution and the two series add.

What is replayed is the rules. The committee is not: a model asked about
2019 already knows 2020, so its judgement cannot be tested on the past.
The swing leg takes the top-ranked candidate that passes the gates, which
is what the committee has done so far in the live record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from midas.research import strategies
from midas.research.data import PriceData
from midas.research.metrics import cagr, calendar_year_returns, max_drawdown
from midas.research.portfolio import simulate
from midas.research.swing_clone import CloneStats, midas_clone

SLEEVE = "_TBILL_SLEEVE"  # the cash proxy held as an asset inside the core allocation
ACCOUNT = "Midas as configured (core + swing)"


@dataclass(frozen=True)
class SwingRules:
    """The swing book's rules that daily bars can express. Defaults are the
    live configuration of 2026-11-24."""

    risk_pct: float = 1.0  # PER_TRADE_RISK_PCT
    notional_cap_pct: float = 13.0  # MAX_NOTIONAL_PCT_PER_TRADE
    max_holding_days: int = 21  # MAX_HOLDING_DAYS
    breakeven_r: float = 1.0  # BREAKEVEN_AT_R
    trail_days: int = 10  # TRAIL_LOOKBACK_DAYS
    require_reclaim: bool = True  # SETUP_GATE_REQUIRE_RECLAIM
    stop_atr: float = 1.0  # MIN_STOP_ATR, under the swing low
    min_room: float = 1.5  # MIN_REWARD_TO_RISK to the 20-day high
    entry_wait_days: int = 5  # ENTRY_REQUIRE_STRENGTH: sessions a thesis waits
    max_index_gap_pct: float = 0.5  # ENTRY_MAX_INDEX_DROP_PCT
    regime: bool = True  # REGIME_FILTER_ENABLED
    throttle: float = 0.0  # REGIME_THROTTLE
    trial_trades: int = 20  # SWING_TRIAL_TRADES
    committee_cap: int = 4  # AUTO_CYCLE_MAX_COMMITTEES
    cost_bps: float = 10.0

    def clone_kwargs(self, budget_pct: float = 15.0) -> dict[str, Any]:
        """Keywords for `midas_clone`. The budget and the per-trade cap decide
        how many positions fit; at least one."""
        slots = max(1, int(budget_pct // self.notional_cap_pct)) if self.notional_cap_pct else 1
        return {
            "time_stop_days": self.max_holding_days,
            "risk_pct": self.risk_pct,
            "max_positions": slots,
            "max_new_per_day": 1,
            "notional_cap": self.notional_cap_pct / 100.0,
            "total_cap": budget_pct / 100.0,
            "regime": self.regime,
            "throttle": self.throttle,
            "breakeven_r": self.breakeven_r,
            "trail_days": self.trail_days,
            "cost_bps": self.cost_bps,
            "require_reclaim": self.require_reclaim,
            "stop_atr": self.stop_atr,
            "min_room": self.min_room,
            "entry_wait_days": self.entry_wait_days,
            "max_index_gap_pct": self.max_index_gap_pct,
            "trial_trades": self.trial_trades,
            "committee_cap": self.committee_cap,
        }


@dataclass(frozen=True)
class AccountConfig:
    core_universe: tuple[str, ...] = ("SPY", "QQQ", "EFA", "GLD", "IEF")
    core_allocation_pct: float = 85.0
    core_trend_window: int = 200
    cash_sleeve: bool = True  # CORE_CASH_TICKER set: the off-trend share earns T-bills
    swing_budget_pct: float | None = None  # None: what the core allocation leaves
    swing: SwingRules = field(default_factory=SwingRules)
    etf_cost_bps: float = 5.0

    @property
    def budget_pct(self) -> float:
        left = max(0.0, 100.0 - self.core_allocation_pct)
        return left if self.swing_budget_pct is None else min(self.swing_budget_pct, left)


def core_returns(data: PriceData, cfg: AccountConfig) -> pd.Series:
    """Daily return the core book adds to the account. The share of the
    account outside the core allocation earns nothing here: it is the
    swing book's cash."""
    universe = list(cfg.core_universe)
    share = cfg.core_allocation_pct / 100.0
    weights = strategies.tactical_sma(data, universe, window=cfg.core_trend_window) * share
    rets = data.returns[universe]
    if cfg.cash_sleeve:
        weights = weights.assign(**{SLEEVE: share - weights.sum(axis=1)})
        rets = rets.assign(**{SLEEVE: data.cash_daily})
    return simulate(weights, rets, data.cash_daily * 0.0, cost_bps=cfg.etf_cost_bps)


@dataclass
class Replay:
    config: AccountConfig
    returns: dict[str, pd.Series]  # account, core, swing, spy, cash: one shared index
    swing: CloneStats

    @property
    def years(self) -> float:
        return len(self.returns["account"]) / 252.0

    def _names(self) -> list[tuple[str, str]]:
        cfg = self.config
        idle = "off-trend share in T-bills" if cfg.cash_sleeve else "idle cash at zero"
        return [
            (ACCOUNT, "account"),
            (f"Core book: {cfg.core_allocation_pct:g}% of equity, {idle}", "core"),
            (f"Swing book: {cfg.budget_pct:g}% budget, the rules as code", "swing"),
            ("Buy & hold SPY", "spy"),
            ("T-bills", "cash"),
        ]

    def table(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for name, key in self._names():
            r = self.returns[key]
            growth = float((1.0 + r).prod())
            yearly = calendar_year_returns(r)
            rows.append(
                {
                    "book": name,
                    "cagr": cagr(r),
                    "total_return": growth - 1.0,
                    "max_drawdown": max_drawdown(r),
                    "worst_year": float(yearly.min()) if len(yearly) else float("nan"),
                    "growth_of_1k": 1000.0 * growth,
                }
            )
        return pd.DataFrame(rows)

    def by_year(self) -> pd.DataFrame:
        cols = {"midas": "account", "spy": "spy", "t_bills": "cash"}
        frame = pd.DataFrame({k: calendar_year_returns(self.returns[v]) for k, v in cols.items()})
        frame.insert(0, "year", [int(y) for y in frame.index])
        return frame.reset_index(drop=True)


def replay(data: PriceData, cfg: AccountConfig, *, start: str | None = None) -> Replay:
    """Both books over the same days, from `start` or from the first day
    the core book can read its trend, whichever is later."""
    core = core_returns(data, cfg)
    begin = core.index[0]
    if start is not None:
        begin = max(begin, pd.Timestamp(start))
    core = core[core.index >= begin]
    universe = data.available(data.universes.get("sp500", []))
    if universe and cfg.budget_pct > 0:
        swing, stats = midas_clone(
            data, universe, start=str(begin.date()), **cfg.swing.clone_kwargs(cfg.budget_pct)
        )
    else:
        swing, stats = pd.Series(dtype=float), CloneStats()
    idx = core.index

    def on(series: pd.Series) -> pd.Series:
        return series.reindex(idx).fillna(0.0)

    swing = on(swing)
    return Replay(
        config=cfg,
        returns={
            "account": core + swing,
            "core": core,
            "swing": swing,
            "spy": on(data.returns["SPY"]),
            "cash": on(data.cash_daily),
        },
        swing=stats,
    )


def _pct(x: float, digits: int = 1) -> str:
    return "" if pd.isna(x) else f"{x * 100:.{digits}f}%"


def _table(header: Sequence[str], cells: Sequence[Sequence[str]]) -> str:
    out = "| " + " | ".join(header) + " |\n|" + "---|" * len(header) + "\n"
    return out + "".join("| " + " | ".join(row) + " |\n" for row in cells)


def render(result: Replay, *, capital: float = 1000.0, committee_cost_usd: float = 0.14) -> str:
    """The replay as markdown: the books, the swing trades, the years, and
    what a daily-bar replay of rules leaves out."""
    cfg, sw, rules = result.config, result.swing, result.config.swing
    idx = result.returns["account"].index
    idle = "the off-trend share in T-bills" if cfg.cash_sleeve else "the off-trend share idle"
    parts = [
        f"# Midas as configured, replayed {idx[0].date()} to {idx[-1].date()} "
        f"({result.years:.1f} years)\n\n",
        f"- Core book: {cfg.core_allocation_pct:g}% of equity in equal slices of "
        f"{', '.join(cfg.core_universe)}, each held only above its {cfg.core_trend_window}-day "
        f"average, rebalanced monthly; {idle}.\n",
        f"- Swing book: {cfg.budget_pct:g}% budget in positions of up to "
        f"{rules.notional_cap_pct:g}%. A pullback in an uptrend"
        + (", reclaimed" if rules.require_reclaim else "")
        + (
            f", stop under the swing low (at least {rules.stop_atr:g} ATR)"
            if rules.stop_atr > 0
            else ", 5% stop"
        )
        + (f", {rules.min_room:g}R of room to the 20-day high" if rules.min_room > 0 else "")
        + (
            f", bought on strength inside {rules.entry_wait_days} sessions"
            if rules.entry_wait_days > 0
            else ", bought at the next open"
        )
        + (
            ", no entries while SPY is below its 200-day"
            if rules.regime and rules.throttle <= 0
            else ""
        )
        + f", {rules.max_holding_days}-session time stop.\n",
        f"- Costs: {cfg.etf_cost_bps:g} bp a side on ETFs, {rules.cost_bps:g} bp on stocks. "
        "The swing universe is today's S&P 500, which flatters it.\n\n",
    ]
    table = result.table()
    parts.append(
        _table(
            ("book", "return a year", "total", "worst drawdown", "worst year", "$1,000 becomes"),
            [
                (
                    str(r.book),
                    _pct(r.cagr),
                    _pct(r.total_return, 0),
                    _pct(r.max_drawdown),
                    _pct(r.worst_year),
                    f"${r.growth_of_1k:,.0f}",
                )
                for r in table.itertuples(index=False)
            ],
        )
    )
    if sw.n_trades:
        exits = ", ".join(f"{k} {v}" for k, v in sorted(sw.exits.items()))
        parts.append(
            f"\nSwing trades: {sw.n_trades} closed, {sw.win_rate * 100:.0f}% winners, "
            f"average {sw.avg_pct:+.2f}% a trade over {sw.avg_days:.0f} sessions "
            f"(exits: {exits}). The book held {sw.exposure * 100:.1f}% of the account on "
            "average.\n"
        )
    else:
        parts.append("\nSwing trades: none.\n")
    if rules.trial_trades > 0:
        read = sw.trade_pcts[: rules.trial_trades]
        average = sum(read) / len(read) if read else float("nan")
        if sw.trial_ended:
            parts.append(
                f"The trial ended the swing book on {sw.trial_ended}: its first "
                f"{rules.trial_trades} trades averaged {average:+.2f}%.\n"
            )
        elif len(read) >= rules.trial_trades:
            parts.append(
                f"The trial passed: the first {rules.trial_trades} trades averaged "
                f"{average:+.2f}%, so the book kept trading.\n"
            )
        else:
            parts.append(
                f"The trial did not finish: {len(read)} of {rules.trial_trades} trades closed.\n"
            )
    if sw.committees and capital > 0:
        spend = sw.committees * committee_cost_usd
        parts.append(
            f"Model spend, not deducted above: about {sw.committees:,} committees would have "
            f"run (up to {rules.committee_cap} gate-passers a day) - ${spend:,.0f} at "
            f"${committee_cost_usd:.2f} each, {spend / result.years / capital * 100:.1f}% a "
            f"year of a ${capital:,.0f} account.\n"
        )
    years = result.by_year()
    parts.append("\n")
    parts.append(
        _table(
            ("year", "midas", "spy", "t_bills"),
            [
                (str(r.year), _pct(r.midas), _pct(r.spy), _pct(r.t_bills))
                for r in years.itertuples(index=False)
            ],
        )
    )
    parts.append(
        "\n## What this does not replay\n"
        "- The committee. A model asked about a past year already knows what came next, so "
        "its judgement cannot be tested on history. The swing leg takes the top-ranked "
        "candidate that passes the gates.\n"
        "- The earnings window: the data set has no report dates, so entries the live gate "
        "would refuse inside it are taken here.\n"
        "- Anything inside a session. Stops and fills are read from daily bars; an order "
        "that opens under the prior close and trades back through it is filled there.\n"
        "- Names that left the index. The swing universe is today's list, which lifts its "
        "result; the ETF books carry no such bias.\n"
    )
    return "".join(parts)
