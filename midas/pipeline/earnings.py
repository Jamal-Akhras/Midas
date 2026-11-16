"""The next earnings date as a committee run recorded it.

The catalyst and fundamentals agents write the date the earnings tool
returned, in whatever form the model chose ("2026-11-17", "January 25,
2027"). The entry rules need a number of days, read from the run's OWN
artifacts - not the latest for the ticker - so the verdict, the risk
check and the execution all judge the same record.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy.orm import Session

from midas.models import ArtifactType
from midas.pipeline.runner import existing_artifact, latest_artifact_for_ticker

DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%m/%d/%Y")


def parse_date(text: object) -> date | None:
    """ISO first, then the spellings the agents actually write; None for
    anything else (an unknown date is unknown, not today)."""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _code_date(screen_context: object) -> date | None:
    """The calendar tool's next report date as the screen context recorded
    it in its technical block (code-sourced; 2026-11-16)."""
    content = getattr(screen_context, "content", None) or {}
    technical = content.get("technical") or {}
    return parse_date(technical.get("next_earnings_date"))


def days_away(db: Session, run_id: uuid.UUID, today: date | None = None) -> tuple[int, str] | None:
    """(days until earnings, ISO date): the screen context's code-sourced
    calendar date first, then the run's catalysts artifact, then its
    fundamentals artifact; None when none of them names a date."""
    today = today or date.today()
    d = _code_date(existing_artifact(db, run_id, ArtifactType.screen_context))
    if d is not None:
        return (d - today).days, d.isoformat()
    for art_type, key in (
        (ArtifactType.catalysts, "upcoming_earnings_date"),
        (ArtifactType.fundamentals, "next_earnings_date"),
    ):
        art = existing_artifact(db, run_id, art_type)
        if art is None:
            continue
        d = parse_date((art.content or {}).get(key))
        if d is not None:
            return (d - today).days, d.isoformat()
    return None


def next_date_for_ticker(db: Session, ticker: str) -> str:
    """The next earnings date on record for a ticker as an ISO string, or
    "": the latest screen context's code-sourced date first, then the
    latest catalysts and fundamentals artifacts (the review trigger)."""
    d = _code_date(latest_artifact_for_ticker(db, ticker, ArtifactType.screen_context))
    if d is not None:
        return d.isoformat()
    for art_type, key in (
        (ArtifactType.catalysts, "upcoming_earnings_date"),
        (ArtifactType.fundamentals, "next_earnings_date"),
    ):
        art = latest_artifact_for_ticker(db, ticker, art_type)
        if art is None:
            continue
        d = parse_date((art.content or {}).get(key))
        if d is not None:
            return d.isoformat()
    return ""


def inside_entry_window(earnings: tuple[int, str] | None, window_days: int) -> bool:
    """True when earnings fall inside [today, today + window]; a reported
    date (negative days) is outside."""
    if earnings is None or window_days <= 0:
        return False
    return 0 <= earnings[0] <= window_days
