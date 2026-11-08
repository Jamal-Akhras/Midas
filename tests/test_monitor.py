def test_portfolio_review_skips_empty_book(factory, demo_router):
    from midas.scheduler.monitor import maybe_portfolio_review

    assert maybe_portfolio_review(factory, demo_router) is None


def test_portfolio_action_schema_normalizes_and_guards():
    import pytest

    from midas.agents.schemas import PortfolioAction

    a = PortfolioAction(ticker="nvda", decision="SELL", reasons=["thesis broken"])
    assert (a.ticker, a.decision) == ("NVDA", "exit")
    assert PortfolioAction(ticker="A", decision="keep").decision == "hold"
    with pytest.raises(ValueError):
        PortfolioAction(ticker="A", decision="exit", reasons=[])
    with pytest.raises(ValueError):
        PortfolioAction(ticker="A", decision="double down", reasons=["x"])
