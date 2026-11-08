"""Live mode must never serve demo fixtures: a missing data key fails
loudly so the gap lands in 'unverified', not in the dossier as fact."""

import pytest

from midas.config import settings
from midas.tools.base import ToolError
from midas.tools.fundamentals_news import get_earnings, get_news
from midas.tools.web import web_search


@pytest.fixture()
def live_no_data_keys(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")  # live mode on
    # every data key, including Alpaca (a news source): the suite must not
    # depend on what the operator's .env happens to contain
    for key in (
        "finnhub_api_key",
        "fmp_api_key",
        "brave_api_key",
        "tavily_api_key",
        "alpaca_api_key",
        "alpaca_api_secret",
    ):
        monkeypatch.setattr(settings, key, "")


def test_earnings_raises_without_keys(live_no_data_keys):
    with pytest.raises(ToolError, match="FINNHUB_API_KEY"):
        get_earnings("ORCL")


def test_news_raises_without_keys(live_no_data_keys):
    with pytest.raises(ToolError, match="news source"):
        get_news("ORCL")


def test_web_search_raises_without_keys(live_no_data_keys):
    with pytest.raises(ToolError, match="web search"):
        web_search("ORCL management quality")


def test_demo_mode_still_serves_fixtures(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    assert get_earnings("ACME").source == "demo"
    assert get_news("ACME").source == "demo"
