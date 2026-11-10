"""SEC EDGAR: filings and insider transactions. Free, no key required.
SEC asks for a descriptive User-Agent on all requests.

Insider DIRECTION comes from Finnhub's transaction codes when that key is
configured; EDGAR's submissions feed only lists Form 4 filings, and a list of
filings is a count, not evidence of buying or selling."""

import logging
from datetime import date, timedelta

import httpx

from midas.config import settings
from midas.tools import demo_data
from midas.tools.base import ToolError, ToolResult

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Midas personal research bot (contact: operator@localhost)"}
_cik_cache: dict[str, str] = {}

FORM_SUMMARY_HINT = {
    "10-K": "annual report (risk factors, segments)",
    "10-Q": "quarterly report",
    "8-K": "material event",
    "DEF 14A": "proxy (board, management incentives)",
    "4": "insider transaction",
}


def _cik_for(ticker: str) -> str:
    if not _cik_cache:
        r = httpx.get("https://www.sec.gov/files/company_tickers.json", headers=UA, timeout=30)
        r.raise_for_status()
        for row in r.json().values():
            _cik_cache[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    cik = _cik_cache.get(ticker.upper())
    if cik is None:
        raise ToolError(f"no CIK found for ticker {ticker}")
    return cik


def get_filings(ticker: str, forms: str = "10-K,10-Q,8-K,DEF 14A", limit: int = 12) -> ToolResult:
    """Recent SEC filings with URLs. forms is a comma-separated filter."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.EDGAR_FILINGS["default"], source="demo")
    wanted = {f.strip().upper() for f in forms.split(",") if f.strip()}
    try:
        cik = _cik_for(ticker)
        r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=UA, timeout=30)
        r.raise_for_status()
        recent = r.json()["filings"]["recent"]
        out = []
        for form, filed, accession, doc in zip(
            recent["form"],
            recent["filingDate"],
            recent["accessionNumber"],
            recent["primaryDocument"],
            strict=True,
        ):
            if wanted and form.upper() not in wanted:
                continue
            acc = accession.replace("-", "")
            out.append(
                {
                    "form": form,
                    "filed": filed,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
                    "summary": FORM_SUMMARY_HINT.get(form.upper(), ""),
                }
            )
            if len(out) >= limit:
                break
        return ToolResult(data=out, source="edgar")
    except httpx.HTTPError as exc:
        raise ToolError(f"EDGAR request failed: {type(exc).__name__}") from exc


INSIDER_WINDOW_DAYS = 90
TRANSACTION_CODES = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant or award",
    "M": "option exercise / conversion",
    "F": "shares withheld for tax",
    "G": "gift",
}
NO_DIRECTION_NOTE = "filing count only; not evidence of buying or selling"
DIRECTION_NOTE = (
    "Only P (purchase) and S (sale) are discretionary open-market trades. A sale that "
    "follows an M (exercise) or sits beside an F (tax withholding) is compensation flow, "
    "not a view on the stock; scheduled 10b5-1 sales also appear as S. Derivative rows "
    "(options, RSUs) are excluded from the share counts."
)


def get_insider_transactions(ticker: str, limit: int = 10) -> ToolResult:
    """Insider activity WITH direction when a source provides it: Finnhub
    transaction codes over the last 90 days (net shares bought vs sold), else
    the EDGAR Form 4 filing list, explicitly marked as carrying no direction.
    The committee once read a bare filing list as "active insider selling"
    (GOOGL, 2026-11-10); the tool now says which kind of answer it is."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.INSIDER_TRANSACTIONS["default"], source="demo")
    if settings.finnhub_api_key:
        try:
            return ToolResult(data=_finnhub_insiders(ticker, limit), source="finnhub")
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "finnhub insider transactions failed for %s: %s", ticker, type(exc).__name__
            )
    return ToolResult(data=_edgar_form4_list(ticker, limit), source="edgar")


def _finnhub_insiders(ticker: str, limit: int, today: date | None = None) -> dict:
    today = today or date.today()
    r = httpx.get(
        "https://finnhub.io/api/v1/stock/insider-transactions",
        params={
            "symbol": ticker,
            "from": (today - timedelta(days=INSIDER_WINDOW_DAYS)).isoformat(),
            "to": today.isoformat(),
            "token": settings.finnhub_api_key,
        },
        timeout=15,
    )
    r.raise_for_status()
    raw = r.json().get("data") or []
    rows = [x for x in raw if not x.get("isDerivative")]
    counts: dict[str, int] = {}
    net_all = 0.0
    bought = 0.0
    sold = 0.0
    for x in rows:
        code = str(x.get("transactionCode") or "?").upper()
        counts[code] = counts.get(code, 0) + 1
        change = float(x.get("change") or 0)
        net_all += change
        if code == "P" and change > 0:
            bought += change
        elif code == "S" and change < 0:
            sold += -change
    rows.sort(key=lambda x: str(x.get("transactionDate") or ""), reverse=True)
    return {
        "ticker": ticker,
        "direction_known": True,
        "window_days": INSIDER_WINDOW_DAYS,
        "counts_by_code": counts,
        "open_market_bought_shares": bought,
        "open_market_sold_shares": sold,
        "net_open_market_shares": bought - sold,
        "net_shares_all_codes": net_all,
        "derivative_rows_excluded": len(raw) - len(rows),
        "rows": [
            {
                "name": x.get("name", ""),
                "transaction_code": str(x.get("transactionCode") or "?").upper(),
                "change": x.get("change"),
                "shares_after": x.get("share"),
                "price": x.get("transactionPrice"),
                "transaction_date": x.get("transactionDate", ""),
                "filing_date": x.get("filingDate", ""),
            }
            for x in rows[:limit]
        ],
        "codes": dict(TRANSACTION_CODES),
        "note": DIRECTION_NOTE,
    }


def _edgar_form4_list(ticker: str, limit: int) -> dict:
    """Recent Form 4 filings with URLs. No direction, no size: a count."""
    try:
        cik = _cik_for(ticker)
        r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=UA, timeout=30)
        r.raise_for_status()
        recent = r.json()["filings"]["recent"]
        out = []
        for form, filed, accession, doc in zip(
            recent["form"],
            recent["filingDate"],
            recent["accessionNumber"],
            recent["primaryDocument"],
            strict=True,
        ):
            if form != "4":
                continue
            acc = accession.replace("-", "")
            out.append(
                {
                    "form": "4",
                    "filed": filed,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
                }
            )
            if len(out) >= limit:
                break
        return {
            "ticker": ticker,
            "direction_known": False,
            "note": f"{NO_DIRECTION_NOTE}; open a filing URL with fetch_page before "
            "stating whether insiders bought or sold",
            "filings": out,
        }
    except httpx.HTTPError as exc:
        raise ToolError(f"EDGAR request failed: {type(exc).__name__}") from exc
