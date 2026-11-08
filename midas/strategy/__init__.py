"""Strategy modules that trade on the paper account.

`core` is the rules-based return engine: a monthly, trend-filtered ETF
allocation kept in its own book, routed through the same autonomy ladder,
broker mirroring and audit trail as the swing committee, and vetoed only by
structured event flags the committee's analyst produces (see
midas.pipeline.events). The research package measured the rules first; the
module trades exactly those rules.
"""
