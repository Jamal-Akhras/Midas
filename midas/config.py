"""Midas configuration. Reads .env; every value overridable via env vars
(prefix MIDAS_ not required - names below are the env var names, lowercased).

The autonomy ladder is a CONFIG CHANGE the operator makes manually after
reviewing outcome stats - never something the system grants itself.
Levels above 2 are rejected here on purpose: the real-money code path does
not exist in this codebase yet (non-negotiable #1).
"""

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Statistical bar that must be met (and manually verified) before anyone
# even writes the L3 real-money path. Kept as data so `midas stats` can
# report progress against it.
L3_BAR = {
    "min_days_at_l2": 30,
    "min_closed_positions": 20,
    "require_positive_expectancy": True,
    # bootstrap probability that cohort expectancy > 0 (not just the point
    # estimate: 20 marginally green trades must not pass on luck)
    "min_p_expectancy_positive": 0.90,
    "max_drawdown_pct": 15.0,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True)

    database_url: str = "sqlite:///midas.db"

    # --- model providers (any one enables real AI; none => demo mode) ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    demo_mode: bool = False  # force demo even with keys present

    # --- data / tool providers (all free tiers; empty => demo fixtures) ---
    # Accepts Alpaca's own dashboard naming too (ALPACA_PAPER_KEY_ID /
    # ALPACA_PAPER_SECRET) so keys can be pasted as-is.
    alpaca_api_key: str = Field(
        "", validation_alias=AliasChoices("alpaca_api_key", "ALPACA_API_KEY", "ALPACA_PAPER_KEY_ID")
    )
    alpaca_api_secret: str = Field(
        "",
        validation_alias=AliasChoices(
            "alpaca_api_secret", "ALPACA_API_SECRET", "ALPACA_PAPER_SECRET"
        ),
    )
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    # Mirror equity paper orders to the real Alpaca paper account (real
    # fills feed the ledger; the Alpaca dashboard becomes an external audit).
    alpaca_mirror_orders: bool = True
    finnhub_api_key: str = ""
    fmp_api_key: str = ""
    brave_api_key: str = ""
    tavily_api_key: str = ""

    # --- autonomy ladder (see module docstring) ---
    # 0 = committee recommends, operator approves everything
    # 1 = approved theses auto-execute on PAPER; exits still proposed
    # 2 = Investor Agent manages entries AND exits on PAPER within caps
    autonomy_level: int = 0

    # --- risk caps (enforced in code at recommendation AND execution) ---
    per_trade_risk_pct: float = 1.0  # max % of account risked per trade
    # Gap protection: stop-based sizing assumes stops hold; an overnight gap
    # through the stop loses (gap/notional), so notional itself is capped.
    max_notional_pct_per_trade: float = 15.0
    # Portfolio exposure budget: total open notional (shares cost + option
    # premium) may not exceed this - the account is never fully invested.
    max_total_notional_pct: float = 60.0
    # Stop protection: move stop to entry once up this many R (0 = off);
    # after breakeven, trail below the N-day low.
    breakeven_at_r: float = 1.0
    trail_lookback_days: int = 10
    # Deterministic time stop: exit positions held this many days (0 = off)
    max_holding_days: int = 21
    # Rest a GTC stop order at Alpaca so stops hold while the monitor is down
    alpaca_standing_stops: bool = True
    # Auto-cycle only during US market hours (chains/quotes are honest then)
    auto_cycle_market_hours_only: bool = True
    # Discovery: merge Alpaca most-actives/movers into the screen universe
    discovery_enabled: bool = True
    discovery_top: int = 10
    # Premarket gap sweep: flag held tickers gapping this % vs prev close
    # for review at the open (0 = off). No orders premarket, ever.
    premarket_gap_alert_pct: float = 3.0
    # Evidence integrity: when Alpaca mirroring is configured, a failed
    # broker fill REJECTS the order (retried next pass) instead of silently
    # simulating - simulated fills must never contaminate the L2 record.
    require_broker_fills: bool = True
    # Mirror options (incl. multi-leg spreads) to the real Alpaca paper
    # account. Requires options trading Level 3 on the paper account for
    # spreads; degrades to the simulator (with friction) when unavailable.
    alpaca_mirror_options: bool = True
    # Paper account seed (applied when the DB is first created) - set this
    # to the size you would HONESTLY fund at L3, so evidence is collected
    # at true scale.
    paper_starting_cash: float = 100_000.0
    # Fractional (notional) share sizing: "$150 of AAPL" instead of whole
    # shares. Caveat: fractional mirror orders are market-only and get NO
    # resting GTC stop at Alpaca - the monitor's passes are their stop layer.
    fractional_shares: bool = True
    # Regime gate: SPY below its 200-day average scales ALL risk budgets by
    # regime_throttle. 0.0 = binary block (no entries, no research spend),
    # 0.5 = half-throttle (entries allowed at half risk), 1.0 = ignore.
    # Exits and stops always run regardless.
    regime_filter_enabled: bool = True
    regime_throttle: float = 0.5
    # Core book (midas.strategy.core): a rules-based, trend-filtered ETF
    # allocation in its own book, rebalanced monthly through the ladder.
    # Off by default; the backtests in docs/results.md are the case for it.
    core_enabled: bool = False
    core_universe: str = "SPY,QQQ,EFA,GLD,IEF"
    core_allocation_pct: float = 60.0  # share of equity the core book may hold
    core_trend_window: int = 200  # days; a slice is held only above this average
    core_veto_days: int = 30  # a high-severity negative flag vetoes a slice this long
    # Event analyst cadence (EVENT_SCAN_WEEKDAYS=mon): scans core universe +
    # watchlist once per listed day. Empty = manual (`midas flags scan`).
    event_scan_weekdays: str = ""
    # Full-market deep scan: deterministic pre-filter over every tradable
    # US common stock before the LLM screener. $0 AI; ~250 API calls/day.
    deep_scan_enabled: bool = True
    deep_scan_min_price: float = 5.0
    deep_scan_min_dollar_volume: float = 20_000_000.0
    deep_scan_max_bars: int = 150  # survivors that get full bar analysis
    deep_scan_keep: int = 15  # setups handed to the LLM screener
    # Option fill friction for SIMULATOR fills (broker fills are real): pay
    # through the mid by this % on entry, give it up on exit, plus fees.
    option_slippage_pct: float = 1.0
    option_fee_per_contract: float = 0.65
    # Correlated-dollar cap: max notional per sector as % of equity
    max_sector_notional_pct: float = 35.0
    max_premium_pct_per_trade: float = 2.0  # max option premium per trade, % of account
    max_total_premium_pct: float = 10.0  # max total open option premium, % of account
    daily_loss_limit_pct: float = 3.0  # realized+unrealized; halts pipeline
    weekly_loss_limit_pct: float = 6.0
    max_open_positions: int = 8
    max_positions_per_sector: int = 3
    min_dte_at_entry: int = 14
    preferred_dte_min: int = 30
    preferred_dte_max: int = 60
    min_open_interest: int = 200
    max_bid_ask_spread_pct: float = 10.0  # (ask-bid)/mid * 100
    min_price_usd: float = 5.0  # screener floor: no penny stocks
    min_avg_daily_volume: int = 1_000_000

    # --- AI spend ---
    daily_spend_limit_usd: float = 10.0  # hard ceiling per UTC day; <=0 disables
    per_stage_cost_ceiling_usd: float = 0.60  # max spend for one agent stage

    # --- scheduling ---
    reeval_hour_utc: int = 21  # daily post-close re-evaluation (US close 20/21 UTC)
    earnings_lookahead_days: int = 7  # event trigger window
    dte_exit_review_threshold: int = 10  # options with fewer DTE get flagged every review

    # --- autopilot cadence (budget-aware) ---
    # Comma-separated weekdays (mon,tue,...) when the monitor auto-runs a
    # screen + committees on gate-passers. Empty = manual only.
    auto_cycle_weekdays: str = ""
    # Max committees one auto-cycle may spawn (highest screener score
    # first). Bounds the day's spend no matter how generous the screen is.
    auto_cycle_max_committees: int = 4

    # --- screener universe ---
    universe: str = "demo"  # "demo", "sp500", or comma-separated tickers
    watchlist: str = ""  # extra comma-separated tickers

    @field_validator("autonomy_level")
    @classmethod
    def _cap_autonomy(cls, v: int) -> int:
        if not 0 <= v <= 2:
            raise ValueError(
                "autonomy_level must be 0-2. Levels 3+ (real money) are not "
                "implemented: the real-order code path intentionally does not "
                "exist until the L3 statistical bar is met and reviewed. "
                f"Bar: {L3_BAR}"
            )
        return v

    @property
    def effective_demo_mode(self) -> bool:
        return self.demo_mode or not (
            self.openai_api_key or self.anthropic_api_key or self.gemini_api_key
        )

    @property
    def watchlist_tickers(self) -> list[str]:
        return [t.strip().upper() for t in self.watchlist.split(",") if t.strip()]


settings = Settings()


# ---------------------------------------------------------------------------
# Operator-editable settings (the UI Settings tab). SECRETS ARE EXCLUDED BY
# DESIGN: API keys are never readable or writable through the web UI.
# "fingerprint" marks strategy-defining fields - changing one restarts the
# L2 evidence clock (see monitor._strategy_fingerprint).
# ---------------------------------------------------------------------------
EDITABLE_SETTINGS: dict[str, dict] = {
    # universe & cadence
    "UNIVERSE": {"attr": "universe", "type": "str", "group": "universe", "fingerprint": True},
    "WATCHLIST": {"attr": "watchlist", "type": "str", "group": "universe", "fingerprint": False},
    "AUTO_CYCLE_WEEKDAYS": {
        "attr": "auto_cycle_weekdays",
        "type": "str",
        "group": "universe",
        "fingerprint": False,
    },
    "AUTO_CYCLE_MAX_COMMITTEES": {
        "attr": "auto_cycle_max_committees",
        "type": "int",
        "group": "universe",
        "fingerprint": True,
    },
    "AUTO_CYCLE_MARKET_HOURS_ONLY": {
        "attr": "auto_cycle_market_hours_only",
        "type": "bool",
        "group": "universe",
        "fingerprint": False,
    },
    "DISCOVERY_ENABLED": {
        "attr": "discovery_enabled",
        "type": "bool",
        "group": "universe",
        "fingerprint": False,
    },
    "DISCOVERY_TOP": {
        "attr": "discovery_top",
        "type": "int",
        "group": "universe",
        "fingerprint": False,
    },
    "PREMARKET_GAP_ALERT_PCT": {
        "attr": "premarket_gap_alert_pct",
        "type": "float",
        "group": "universe",
        "fingerprint": False,
    },
    # autonomy & spend
    "AUTONOMY_LEVEL": {
        "attr": "autonomy_level",
        "type": "int",
        "group": "autonomy",
        "fingerprint": False,
    },
    "DAILY_SPEND_LIMIT_USD": {
        "attr": "daily_spend_limit_usd",
        "type": "float",
        "group": "autonomy",
        "fingerprint": False,
    },
    # risk (all strategy-defining)
    "PER_TRADE_RISK_PCT": {
        "attr": "per_trade_risk_pct",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "MAX_NOTIONAL_PCT_PER_TRADE": {
        "attr": "max_notional_pct_per_trade",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "MAX_TOTAL_NOTIONAL_PCT": {
        "attr": "max_total_notional_pct",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "MAX_SECTOR_NOTIONAL_PCT": {
        "attr": "max_sector_notional_pct",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "MAX_OPEN_POSITIONS": {
        "attr": "max_open_positions",
        "type": "int",
        "group": "risk",
        "fingerprint": True,
    },
    "MAX_POSITIONS_PER_SECTOR": {
        "attr": "max_positions_per_sector",
        "type": "int",
        "group": "risk",
        "fingerprint": True,
    },
    "DAILY_LOSS_LIMIT_PCT": {
        "attr": "daily_loss_limit_pct",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "WEEKLY_LOSS_LIMIT_PCT": {
        "attr": "weekly_loss_limit_pct",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "MAX_HOLDING_DAYS": {
        "attr": "max_holding_days",
        "type": "int",
        "group": "risk",
        "fingerprint": True,
    },
    "BREAKEVEN_AT_R": {
        "attr": "breakeven_at_r",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "TRAIL_LOOKBACK_DAYS": {
        "attr": "trail_lookback_days",
        "type": "int",
        "group": "risk",
        "fingerprint": True,
    },
    # options
    "MIN_DTE_AT_ENTRY": {
        "attr": "min_dte_at_entry",
        "type": "int",
        "group": "options",
        "fingerprint": True,
    },
    "MIN_OPEN_INTEREST": {
        "attr": "min_open_interest",
        "type": "int",
        "group": "options",
        "fingerprint": True,
    },
    "MAX_BID_ASK_SPREAD_PCT": {
        "attr": "max_bid_ask_spread_pct",
        "type": "float",
        "group": "options",
        "fingerprint": True,
    },
    "MAX_PREMIUM_PCT_PER_TRADE": {
        "attr": "max_premium_pct_per_trade",
        "type": "float",
        "group": "options",
        "fingerprint": True,
    },
    "MAX_TOTAL_PREMIUM_PCT": {
        "attr": "max_total_premium_pct",
        "type": "float",
        "group": "options",
        "fingerprint": True,
    },
    "OPTION_SLIPPAGE_PCT": {
        "attr": "option_slippage_pct",
        "type": "float",
        "group": "options",
        "fingerprint": True,
    },
    "OPTION_FEE_PER_CONTRACT": {
        "attr": "option_fee_per_contract",
        "type": "float",
        "group": "options",
        "fingerprint": True,
    },
    # execution
    "ALPACA_MIRROR_ORDERS": {
        "attr": "alpaca_mirror_orders",
        "type": "bool",
        "group": "execution",
        "fingerprint": False,
    },
    "ALPACA_STANDING_STOPS": {
        "attr": "alpaca_standing_stops",
        "type": "bool",
        "group": "execution",
        "fingerprint": False,
    },
    "REQUIRE_BROKER_FILLS": {
        "attr": "require_broker_fills",
        "type": "bool",
        "group": "execution",
        "fingerprint": False,
    },
    "ALPACA_MIRROR_OPTIONS": {
        "attr": "alpaca_mirror_options",
        "type": "bool",
        "group": "execution",
        "fingerprint": False,
    },
    "REGIME_FILTER_ENABLED": {
        "attr": "regime_filter_enabled",
        "type": "bool",
        "group": "risk",
        "fingerprint": True,
    },
    "REGIME_THROTTLE": {
        "CORE_ENABLED": {
            "attr": "core_enabled",
            "type": "bool",
            "group": "core",
            "fingerprint": True,
        },
        "CORE_UNIVERSE": {
            "attr": "core_universe",
            "type": "str",
            "group": "core",
            "fingerprint": True,
        },
        "CORE_ALLOCATION_PCT": {
            "attr": "core_allocation_pct",
            "type": "float",
            "group": "core",
            "fingerprint": True,
        },
        "CORE_TREND_WINDOW": {
            "attr": "core_trend_window",
            "type": "int",
            "group": "core",
            "fingerprint": True,
        },
        "CORE_VETO_DAYS": {
            "attr": "core_veto_days",
            "type": "int",
            "group": "core",
            "fingerprint": True,
        },
        "EVENT_SCAN_WEEKDAYS": {
            "attr": "event_scan_weekdays",
            "type": "str",
            "group": "universe",
            "fingerprint": False,
        },
        "attr": "regime_throttle",
        "type": "float",
        "group": "risk",
        "fingerprint": True,
    },
    "DEEP_SCAN_ENABLED": {
        "attr": "deep_scan_enabled",
        "type": "bool",
        "group": "universe",
        "fingerprint": False,
    },
    "DEEP_SCAN_MIN_DOLLAR_VOLUME": {
        "attr": "deep_scan_min_dollar_volume",
        "type": "float",
        "group": "universe",
        "fingerprint": False,
    },
    "DEEP_SCAN_MAX_BARS": {
        "attr": "deep_scan_max_bars",
        "type": "int",
        "group": "universe",
        "fingerprint": False,
    },
    "DEEP_SCAN_KEEP": {
        "attr": "deep_scan_keep",
        "type": "int",
        "group": "universe",
        "fingerprint": False,
    },
}


def _coerce(value: str, kind: str):
    if kind == "bool":
        if str(value).lower() in ("true", "1", "yes", "on"):
            return True
        if str(value).lower() in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value)


def update_setting(env_key: str, raw_value: str, env_path=None) -> dict:
    """Validate + persist one whitelisted setting: write .env (preserving
    all other lines, secrets untouched) and hot-apply to the live process.
    Returns {key, old, new, fingerprint}. Raises ValueError on bad input."""
    from pathlib import Path

    meta = EDITABLE_SETTINGS.get(env_key.upper())
    if meta is None:
        raise ValueError(f"'{env_key}' is not an editable setting")
    value = _coerce(raw_value, meta["type"])
    old = getattr(settings, meta["attr"])
    # validate BEFORE persisting (e.g. autonomy_level 3 must be rejected)
    setattr(settings, meta["attr"], value)

    path = Path(env_path) if env_path else Path(".env")
    key = env_key.upper()
    line = f"{key}={value}"
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        replaced = False
        for i, ln in enumerate(lines):
            if ln.split("=", 1)[0].strip() == key and not ln.lstrip().startswith("#"):
                if not replaced:
                    lines[i] = line
                    replaced = True
                else:
                    lines[i] = f"# {ln}  # duplicate removed by settings UI"
        if not replaced:
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path.write_text(line + "\n", encoding="utf-8")
    return {"key": key, "old": old, "new": value, "fingerprint": meta["fingerprint"]}


def reload_settings() -> None:
    """Re-read .env into the live settings object (both the monitor and the
    UI call this, so Settings-tab changes apply without restarts)."""
    fresh = Settings()
    for name in fresh.model_fields:
        object.__setattr__(settings, name, getattr(fresh, name))
