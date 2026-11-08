"""SEC EDGAR: filings and insider transactions. Free, no key required.
SEC asks for a descriptive User-Agent on all requests."""

import logging

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


def get_insider_transactions(ticker: str, limit: int = 10) -> ToolResult:
    """Recent Form 4 insider filings (buys/sells) with URLs."""
    ticker = ticker.upper()
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.INSIDER_TRANSACTIONS["default"], source="demo")
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
                    "note": "Form 4 insider transaction; open URL for direction/size",
                }
            )
            if len(out) >= limit:
                break
        return ToolResult(data=out, source="edgar")
    except httpx.HTTPError as exc:
        raise ToolError(f"EDGAR request failed: {type(exc).__name__}") from exc
