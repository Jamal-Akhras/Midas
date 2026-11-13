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
from midas.pipeline.runner import existing_artifact

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


def days_away(db: Session, run_id: uuid.UUID, today: date | None = None) -> tuple[int, str] | None:
    """(days until earnings, ISO date) from the run's catalysts artifact,
    else its fundamentals artifact; None when neither names a date."""
    today = today or date.today()
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


def inside_entry_window(earnings: tuple[int, str] | None, window_days: int) -> bool:
    """True when earnings fall inside [today, today + window]; a reported
    date (negative days) is outside."""
    if earnings is None or window_days <= 0:
        return False
    return 0 <= earnings[0] <= window_days
