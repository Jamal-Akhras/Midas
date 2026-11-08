"""Walk-forward parameter selection: choose a strategy's parameters on a
trailing window, apply them to the next period, roll forward. The stitched
out-of-sample series is what the strategy would have earned without
hindsight about its own best setting."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from midas.research.data import PriceData
from midas.research.metrics import sharpe
from midas.research.portfolio import simulate

WeightFn = Callable[..., pd.DataFrame]
ScoreFn = Callable[[pd.Series, pd.Series], float]


@dataclass(frozen=True)
class WalkForwardResult:
    oos_returns: pd.Series
    choices: pd.DataFrame  # one row per test period: start, end, chosen params, train score
    candidates: dict[str, pd.Series]  # full-history returns per parameter set, for reference


def _label(params: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(params.items()))


def walk_forward(
    data: PriceData,
    strategy: WeightFn,
    grid: Sequence[Mapping[str, Any]],
    *,
    train_years: int = 5,
    test_years: int = 1,
    cost_bps: float = 5.0,
    score: ScoreFn = sharpe,
) -> WalkForwardResult:
    """Each parameter set is simulated once over the full history (weights
    are causal, so slicing the result is valid). For every test period the
    set with the best `score` over the preceding `train_years` is used."""
    returns = data.returns
    candidates = {
        _label(p): simulate(strategy(data, **p), returns, data.cash_daily, cost_bps=cost_bps)
        for p in grid
    }
    years = sorted({int(y) for y in data.index.year})
    pieces: list[pd.Series] = []
    rows: list[dict[str, Any]] = []
    for test_start_year in years[train_years::test_years]:
        train_lo = pd.Timestamp(year=test_start_year - train_years, month=1, day=1)
        test_lo = pd.Timestamp(year=test_start_year, month=1, day=1)
        test_hi = pd.Timestamp(year=test_start_year + test_years, month=1, day=1)
        best_label, best_score = "", float("-inf")
        for label, series in candidates.items():
            train = series[(series.index >= train_lo) & (series.index < test_lo)]
            if len(train) < 200:
                continue
            s = score(train, data.cash_daily)
            if s == s and s > best_score:  # NaN-safe
                best_label, best_score = label, s
        if not best_label:
            continue
        chosen = candidates[best_label]
        test = chosen[(chosen.index >= test_lo) & (chosen.index < test_hi)]
        if test.empty:
            continue
        pieces.append(test)
        rows.append(
            {
                "test_start": str(test.index[0].date()),
                "test_end": str(test.index[-1].date()),
                "chosen": best_label,
                "train_score": round(best_score, 3),
            }
        )
    oos = pd.concat(pieces) if pieces else pd.Series(dtype=float)
    oos.name = "walk_forward"
    return WalkForwardResult(oos_returns=oos, choices=pd.DataFrame(rows), candidates=candidates)
