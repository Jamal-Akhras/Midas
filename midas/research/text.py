"""Text scores for event studies: does what a company said rank what its
stock did next?

The pieces are deliberately plain, because each is a place a study can
leak the future:

- `tone_counts` / `tone`: a word-list score. Nothing is fitted, so there
  is no model that could have seen the test period.
- `signal_day`: the first session that had the text before its close. The
  trade is the next open, so every event waits at least a full session.
- `abnormal_tone`: the score against the same company's earlier calls.
- `trailing_percentile`: the rank among events of the year before, never
  among events on the same day or later.
- `earnings_tone_study`: joins the ranked events to forward returns over
  the same-day universe, in the conventions of `midas.research.events`,
  with the earnings-day price reaction as a control and a shuffled score
  as the placebo.

The protocol these serve, with its pass mark fixed before any result, is
docs/studies/2026-11-earnings-call-tone.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from midas.research.data import PriceData
from midas.research.events import (
    EventRow,
    event_row,
    forward_returns,
    newey_west_t,
    universe_excess,
)
from midas.research.events import liquid as liquid_mask

WORD = re.compile(r"[A-Za-z]+")
CLOSE_HOUR = 16  # New York; a call that starts at or after this belongs to the next session
SCORES = ("abnormal", "tone", "negative", "reaction")


def tone_counts(
    document: str, positive: frozenset[str], negative: frozenset[str]
) -> tuple[int, int]:
    """(positive words, negative words) in the document: whole words, any case."""
    pos = neg = 0
    for word in WORD.findall(document.upper()):
        if word in positive:
            pos += 1
        elif word in negative:
            neg += 1
    return pos, neg


def tone(pos: int, neg: int, *, min_words: int = 50) -> float | None:
    """Net share of positive words among the sentiment words; None when
    there are too few to mean anything."""
    total = pos + neg
    if total < min_words or total == 0:
        return None
    return (pos - neg) / total


def load_word_lists(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """The positive and negative lists of a Loughran-McDonald master
    dictionary CSV (columns Word, Positive, Negative; non-zero = member)."""
    table = pd.read_csv(path, usecols=["Word", "Positive", "Negative"], keep_default_na=False)
    words = table["Word"].astype(str).str.upper()
    return (
        frozenset(words[table["Positive"] != 0]),
        frozenset(words[table["Negative"] != 0]),
    )


def score_transcripts(
    path: Path, positive: frozenset[str], negative: frozenset[str], *, batch_size: int = 500
) -> pd.DataFrame:
    """Word counts for every call in a transcript parquet (columns symbol,
    date, content): one row per call with symbol, ts, pos, neg, chars. The
    text is read in batches and never held whole."""
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    source = pq.ParquetFile(path)
    for batch in source.iter_batches(batch_size=batch_size, columns=["symbol", "date", "content"]):
        for symbol, stamp, content in zip(
            batch.column("symbol").to_pylist(),
            batch.column("date").to_pylist(),
            batch.column("content").to_pylist(),
            strict=True,
        ):
            document = content or ""
            pos, neg = tone_counts(document, positive, negative)
            rows.append(
                {"symbol": symbol, "ts": stamp, "pos": pos, "neg": neg, "chars": len(document)}
            )
    out = pd.DataFrame(rows)
    out["ts"] = pd.to_datetime(out["ts"], errors="coerce")
    return out.dropna(subset=["ts"]).reset_index(drop=True)


def signal_day(
    ts: pd.Timestamp, sessions: pd.DatetimeIndex, *, after_close_same_day: bool = False
) -> pd.Timestamp | None:
    """The session whose close is the first to come after the call began: the
    call's own day when it started before 16:00 on a session, otherwise the
    next session. A stamp of exactly 00:00 is a missing time and waits a
    session. `after_close_same_day` is the faster variant: a call after the
    close of a session is known by the next open, so that session is its
    signal day. None when the data ends first."""
    day = ts.normalize()
    at = int(sessions.searchsorted(day))
    is_session = at < len(sessions) and sessions[at] == day
    timed = ts != day
    before_close = ts < day + pd.Timedelta(hours=CLOSE_HOUR)
    if is_session and timed and (before_close or after_close_same_day):
        return pd.Timestamp(day)
    following = int(sessions.searchsorted(day, side="right"))
    return pd.Timestamp(sessions[following]) if following < len(sessions) else None


def abnormal_tone(frame: pd.DataFrame, *, calls: int = 4, min_calls: int = 2) -> pd.Series:
    """Tone minus the mean tone of the same symbol's previous `calls` calls
    (columns symbol, signal_day, tone). NaN until `min_calls` exist."""
    ordered = frame.sort_values(["symbol", "signal_day"])
    prior = ordered.groupby("symbol")["tone"].transform(
        lambda s: s.shift(1).rolling(calls, min_periods=min_calls).mean()
    )
    return (ordered["tone"] - prior).reindex(frame.index)


def trailing_percentile(
    days: Iterable[Any], values: pd.Series, *, window_days: int = 365, min_events: int = 200
) -> pd.Series:
    """Each event's percentile among the events of the `window_days` before
    its own day: the share of them with a lower value. Events of the same
    day are not in each other's pool, and nothing later is. NaN when the
    pool holds fewer than `min_events`."""
    day = pd.to_datetime(pd.Series(list(days))).to_numpy()
    value = np.asarray(values, dtype=float)
    order = np.argsort(day, kind="stable")
    sorted_days, sorted_values = day[order], value[order]
    out = np.full(len(value), np.nan)
    window = np.timedelta64(window_days, "D")
    for current in np.unique(sorted_days):
        lo = int(np.searchsorted(sorted_days, current - window, side="left"))
        hi = int(np.searchsorted(sorted_days, current, side="left"))
        pool = sorted_values[lo:hi]
        pool = pool[~np.isnan(pool)]
        if len(pool) < min_events:
            continue
        end = int(np.searchsorted(sorted_days, current, side="right"))
        ranked = np.sort(pool)
        todays = sorted_values[hi:end]
        out[order[hi:end]] = np.searchsorted(ranked, todays, side="left") / len(ranked)
    result = pd.Series(out, index=values.index)
    return result.where(~np.isnan(value))


def spread_stat(
    excess: pd.DataFrame, top: pd.DataFrame, bottom: pd.DataFrame, *, lags: int
) -> tuple[float, float, int]:
    """(mean daily top-minus-bottom difference, its Newey-West t, the number
    of days that had both legs)."""
    diff = (excess.where(top).mean(axis=1) - excess.where(bottom).mean(axis=1)).dropna()
    if diff.empty:
        return float("nan"), float("nan"), 0
    return float(diff.mean()), newey_west_t(diff, lags), len(diff)


def build_events(
    calls: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    symbols: Iterable[str],
    *,
    min_chars: int = 5000,
    min_words: int = 50,
    after_close_same_day: bool = False,
) -> pd.DataFrame:
    """Calls (symbol, ts, pos, neg[, chars]) as events: symbol, signal_day,
    tone, negative share. One event per symbol and signal day."""
    keep = calls[calls["symbol"].isin(set(symbols))]
    if "chars" in keep.columns:
        keep = keep[keep["chars"] >= min_chars]
    rows: list[dict[str, Any]] = []
    for row in keep.itertuples(index=False):
        value = tone(int(row.pos), int(row.neg), min_words=min_words)
        day = signal_day(pd.Timestamp(row.ts), sessions, after_close_same_day=after_close_same_day)
        if value is None or day is None:
            continue
        rows.append(
            {
                "symbol": str(row.symbol),
                "ts": pd.Timestamp(row.ts),
                "signal_day": day,
                "tone": value,
                # negative words per 1,000 characters, signed so that higher is
                # better. Not neg / (pos + neg): that is tone again, in other units.
                "negative": -1000.0 * int(row.neg) / max(int(getattr(row, "chars", 0) or 0), 1)
                if "chars" in keep.columns
                else float("nan"),
            }
        )
    out = pd.DataFrame(rows, columns=["symbol", "ts", "signal_day", "tone", "negative"])
    out = out.sort_values(["signal_day", "symbol", "ts"]).drop_duplicates(["symbol", "signal_day"])
    return out.reset_index(drop=True)


def _mask(frame: pd.DataFrame, like: pd.DataFrame) -> pd.DataFrame:
    """True at (signal_day, symbol) for each event in `frame`."""
    out = np.zeros(like.shape, dtype=bool)
    rows = like.index.get_indexer(frame["signal_day"])
    cols = like.columns.get_indexer(frame["symbol"])
    ok = (rows >= 0) & (cols >= 0)
    out[rows[ok], cols[ok]] = True
    return pd.DataFrame(out, index=like.index, columns=like.columns)


@dataclass
class ToneStudy:
    horizon: int
    score: str
    benchmark: str
    events: pd.DataFrame  # symbol, signal_day, tone, score, rank, reaction
    rows: dict[str, EventRow] = field(default_factory=dict)
    recent: dict[str, EventRow] = field(default_factory=dict)
    spread: float = float("nan")
    spread_t: float = float("nan")
    spread_days: int = 0
    reaction_split: list[tuple[str, float, float, int]] = field(default_factory=list)
    placebo: EventRow | None = None


def earnings_tone_study(
    data: PriceData,
    calls: pd.DataFrame,
    *,
    score: str = "abnormal",
    horizon: int = 20,
    start: str = "2010-01-01",
    recent: str = "2020-01-01",
    benchmark: str = "universe",
    window_days: int = 365,
    min_rank_events: int = 200,
    after_close_same_day: bool = False,
    seed: int = 0,
) -> ToneStudy:
    """The protocol of docs/studies/2026-11-earnings-call-tone.md for one
    score, horizon and benchmark. `calls` has symbol, ts (New York), pos,
    neg and optionally chars."""
    if score not in SCORES:
        raise ValueError(f"score must be one of {SCORES}")
    universe = data.available(data.universes.get("sp500", []))
    liquid = liquid_mask(data, universe)
    raw, vs_index = forward_returns(data, universe, horizon)
    excess = universe_excess(raw, liquid) if benchmark == "universe" else vs_index
    day_return = data.close[universe].pct_change(fill_method=None) * 100.0
    reaction = universe_excess(day_return, liquid)

    ev = build_events(calls, data.index, universe, after_close_same_day=after_close_same_day)
    rows_at = data.index.get_indexer(ev["signal_day"])
    cols_at = pd.Index(universe).get_indexer(ev["symbol"])
    ev["reaction"] = reaction.to_numpy()[rows_at, cols_at]
    ev = ev[liquid.to_numpy()[rows_at, cols_at]].reset_index(drop=True)
    if score == "abnormal":
        ev["score"] = abnormal_tone(ev)
    else:
        ev["score"] = ev[score]
    ev = ev.dropna(subset=["score"]).reset_index(drop=True)
    ev["rank"] = trailing_percentile(
        ev["signal_day"], ev["score"], window_days=window_days, min_events=min_rank_events
    )
    ev = ev[(ev["signal_day"] >= pd.Timestamp(start)) & ev["rank"].notna()].reset_index(drop=True)

    top, bottom = ev[ev["rank"] >= 0.8], ev[ev["rank"] <= 0.2]
    middle = ev[(ev["rank"] > 0.2) & (ev["rank"] < 0.8)]
    groups = {
        "top quintile": top,
        "middle three": middle,
        "bottom quintile": bottom,
        "all calls": ev,
    }
    study = ToneStudy(horizon=horizon, score=score, benchmark=benchmark, events=ev)
    for name, group in groups.items():
        mask = _mask(group, raw)
        row = event_row(name, mask, raw, excess, horizon)
        if row is not None:
            study.rows[name] = row
        late = event_row(name, mask, raw, excess, horizon, start=recent)
        if late is not None:
            study.recent[name] = late
    top_mask, bottom_mask = _mask(top, raw), _mask(bottom, raw)
    study.spread, study.spread_t, study.spread_days = spread_stat(
        excess, top_mask, bottom_mask, lags=horizon
    )

    # the market's own verdict on the report, in three groups formed inside each year
    if ev["reaction"].notna().sum() >= 30:
        year = ev["signal_day"].dt.year
        third = ev.groupby(year)["reaction"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=False) if len(s) >= 3 else np.nan
        )
        for k, label in enumerate(("weak reaction", "middle reaction", "strong reaction")):
            part = ev[third == k]
            value, t, days = spread_stat(
                excess,
                _mask(part[part["rank"] >= 0.8], raw),
                _mask(part[part["rank"] <= 0.2], raw),
                lags=horizon,
            )
            study.reaction_split.append((label, value, t, days))

    # placebo: the same scores dealt out at random inside each calendar quarter
    rng = np.random.default_rng(seed)
    shuffled = ev[["symbol", "signal_day", "score"]].copy()
    quarter = shuffled["signal_day"].dt.to_period("Q")
    shuffled["score"] = shuffled.groupby(quarter)["score"].transform(
        lambda s: rng.permutation(s.to_numpy())
    )
    shuffled["rank"] = trailing_percentile(
        shuffled["signal_day"],
        shuffled["score"],
        window_days=window_days,
        min_events=min_rank_events,
    )
    study.placebo = event_row(
        "placebo: shuffled score, top quintile",
        _mask(shuffled[shuffled["rank"] >= 0.8], raw),
        raw,
        excess,
        horizon,
    )
    return study


# ---------- the pass mark, applied mechanically ----------

LONG_LEG_MIN_PCT, LONG_LEG_MIN_T = 0.40, 3.0
RECENT_MIN_T, SPREAD_MIN_T, PLACEBO_MAX_T = 2.0, 2.0, 2.0


@dataclass(frozen=True)
class Verdict:
    criterion: str
    observed: str
    passed: bool


def judge(study: ToneStudy) -> list[Verdict]:
    """The four criteria and the placebo of the protocol, in its order. A
    missing row or statistic is a failed criterion, never a skipped one."""

    def known(x: float) -> bool:
        return x == x  # not NaN

    top, late = study.rows.get("top quintile"), study.recent.get("top quintile")
    out: list[Verdict] = []
    ok = top is not None and top.excess >= LONG_LEG_MIN_PCT and top.t_excess >= LONG_LEG_MIN_T
    out.append(
        Verdict(
            f"long leg: at least +{LONG_LEG_MIN_PCT:.2f}% over the universe, t of 3 or more",
            "no events" if top is None else f"{top.excess:+.2f}%, t {top.t_excess:.1f}",
            bool(ok),
        )
    )
    same_sign = top is not None and late is not None and late.excess * top.excess > 0
    ok = bool(same_sign and late is not None and abs(late.t_excess) >= RECENT_MIN_T)
    out.append(
        Verdict(
            "recent: the same sign since 2020 with t of 2 or more",
            "no events" if late is None else f"{late.excess:+.2f}%, t {late.t_excess:.1f}",
            ok,
        )
    )
    ok = known(study.spread_t) and study.spread > 0 and study.spread_t >= SPREAD_MIN_T
    out.append(
        Verdict(
            "spread: top minus bottom quintile positive, t of 2 or more",
            f"{study.spread:+.2f}%, t {study.spread_t:.1f} over {study.spread_days} days",
            bool(ok),
        )
    )
    parts = [value for _, value, _, _ in study.reaction_split]
    mean_part = float(np.mean(parts)) if parts else float("nan")
    ok = (
        len(parts) == 3
        and all(known(v) and v > 0 for v in parts)
        and known(study.spread)
        and mean_part >= 0.5 * study.spread
    )
    out.append(
        Verdict(
            "beyond the price reaction: spread positive in each reaction group, "
            "their mean at least half the unconditional spread",
            ", ".join(f"{label} {value:+.2f}%" for label, value, _, _ in study.reaction_split)
            or "no groups",
            bool(ok),
        )
    )
    placebo = study.placebo
    ok = placebo is not None and known(placebo.t_excess) and abs(placebo.t_excess) < PLACEBO_MAX_T
    out.append(
        Verdict(
            "placebo: shuffled score's long leg has |t| under 2",
            "no events" if placebo is None else f"{placebo.excess:+.2f}%, t {placebo.t_excess:.1f}",
            bool(ok),
        )
    )
    return out


def passed(study: ToneStudy) -> bool:
    return all(v.passed for v in judge(study))


# ---------- the report ----------

SECONDARY = (
    ("5 sessions", {"horizon": 5}),
    ("60 sessions", {"horizon": 60}),
    ("raw tone level, 20 sessions", {"score": "tone"}),
    ("negative-word share alone, 20 sessions", {"score": "negative"}),
    ("against SPY, 20 sessions", {"benchmark": "SPY"}),
    ("after-close calls entered at the next open, 20 sessions", {"after_close_same_day": True}),
    ("price reaction alone, no text, 20 sessions", {"score": "reaction"}),
)


def _line(cells: Iterable[Any]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |\n"


def tone_report(data: PriceData, calls: pd.DataFrame, **kw: Any) -> str:
    """The primary study, its verdict against the pass mark, and the rows the
    protocol lists as reported but not judged, as markdown."""
    primary = earnings_tone_study(data, calls, **kw)
    ev = primary.events
    span = f"{ev['signal_day'].min().date()} to {ev['signal_day'].max().date()}" if len(ev) else "-"
    out = [
        f"Primary: abnormal tone, {primary.horizon} sessions from the open after the signal day, "
        f"excess over the same-day universe. {len(ev):,} ranked events, {span}.\n\n",
        _line(
            ("group", "events", "days", "net return %", "excess %", "t", "since 2020 excess %", "t")
        ),
        "|---|---|---|---|---|---|---|---|\n",
    ]
    for name, row in primary.rows.items():
        late = primary.recent.get(name)
        out.append(
            _line(
                (
                    name,
                    f"{row.signals:,}",
                    row.days,
                    f"{row.raw_net:+.2f}",
                    f"{row.excess:+.2f}",
                    f"{row.t_excess:.1f}",
                    "" if late is None else f"{late.excess:+.2f}",
                    "" if late is None else f"{late.t_excess:.1f}",
                )
            )
        )
    out.append(
        f"\nTop minus bottom quintile: {primary.spread:+.2f}% (t {primary.spread_t:.1f}, "
        f"{primary.spread_days} days with both legs).\n\n"
    )
    out.append("Spread inside each third of the earnings-day price reaction:\n\n")
    out.append(_line(("reaction group", "spread %", "t", "days")) + "|---|---|---|---|\n")
    for label, value, t, days in primary.reaction_split:
        out.append(_line((label, f"{value:+.2f}", f"{t:.1f}", days)))
    out.append("\nAgainst the pass mark:\n\n")
    out.append(_line(("criterion", "observed", "result")) + "|---|---|---|\n")
    for v in judge(primary):
        out.append(_line((v.criterion, v.observed, "pass" if v.passed else "FAIL")))
    out.append(f"\n**Verdict: {'PASS' if passed(primary) else 'FAIL'}.**\n\n")
    out.append("Reported, not judged:\n\n")
    out.append(
        _line(("variant", "events", "long leg excess %", "t", "since 2020 %", "t", "spread %", "t"))
        + "|---|---|---|---|---|---|---|---|\n"
    )
    for name, extra in SECONDARY:
        study = earnings_tone_study(data, calls, **{**kw, **extra})
        top, late = study.rows.get("top quintile"), study.recent.get("top quintile")
        out.append(
            _line(
                (
                    name,
                    f"{len(study.events):,}",
                    "" if top is None else f"{top.excess:+.2f}",
                    "" if top is None else f"{top.t_excess:.1f}",
                    "" if late is None else f"{late.excess:+.2f}",
                    "" if late is None else f"{late.t_excess:.1f}",
                    f"{study.spread:+.2f}",
                    f"{study.spread_t:.1f}",
                )
            )
        )
    return "".join(out)
